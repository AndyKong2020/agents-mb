migrate(app => {
    const ranks = new Collection({
        name: 'experiments', type: 'auth',
        fields: [{name: 'rank', type: 'number', onlyInt: true}],
        indexes: ['CREATE UNIQUE INDEX idx_experiments_rank ON experiments (rank)'],
    })
    app.save(ranks)
    const posts = new Collection({
        name: 'publications', type: 'base',
        listRule: '@request.auth.id != ""', viewRule: '@request.auth.id != ""',
        createRule: '@request.auth.id != "" && @request.body.rank = @request.auth.rank',
        deleteRule: '@request.auth.id != "" && rank = @request.auth.rank',
        fields: [
            {name: 'rank', type: 'number', onlyInt: true},
            {name: 'round', type: 'text', required: true},
            {name: 'score', type: 'number'},
            {name: 'payload', type: 'json'},
            {name: 'created', type: 'autodate', onCreate: true},
        ],
        indexes: [
            'CREATE UNIQUE INDEX idx_publication_key ON publications (rank, round)',
            'CREATE INDEX idx_publication_score ON publications (rank, score, round)',
        ],
    })
    app.save(posts)
    for (const [direction, sort] of [['max', 'DESC'], ['min', 'ASC']]) {
        app.save(new Collection({
            name: 'leaders_' + direction, type: 'view',
            listRule: '@request.auth.id != ""',
            viewQuery: `SELECT p.id, p.rank, p.round, p.score FROM publications p
                WHERE p.id = (SELECT w.id FROM publications w WHERE w.rank = p.rank
                    ORDER BY w.score ${sort}, w.round ASC LIMIT 1)`,
        }))
    }
}, app => {
    for (const name of ['leaders_max', 'leaders_min', 'publications', 'experiments']) {
        app.delete(app.findCollectionByNameOrId(name))
    }
})

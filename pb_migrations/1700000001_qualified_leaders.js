migrate(app => {
    const gate = "json_type(payload, '$.result.qualified') IS 'true'"
    for (const [direction, sort] of [['max', 'DESC'], ['min', 'ASC']]) {
        const view = app.findCollectionByNameOrId('leaders_' + direction)
        view.viewQuery = `SELECT p.id, p.rank, p.round, p.score, p.payload, (${gate}) AS meets_gate
            FROM publications p WHERE p.id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY rank ORDER BY (${gate}) DESC, score ${sort}, round ASC
                    ) AS place FROM publications
                ) WHERE place = 1
            )`
        app.save(view)
    }
}, app => {
    for (const [direction, sort] of [['max', 'DESC'], ['min', 'ASC']]) {
        const view = app.findCollectionByNameOrId('leaders_' + direction)
        view.viewQuery = `SELECT p.id, p.rank, p.round, p.score FROM publications p
            WHERE p.id = (SELECT w.id FROM publications w WHERE w.rank = p.rank
                ORDER BY w.score ${sort}, w.round ASC LIMIT 1)`
        app.save(view)
    }
})

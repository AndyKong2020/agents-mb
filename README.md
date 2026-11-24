# agents-mb

并行实验的成果榜单和工作区同步工具。每个实验使用稳定的 rank 编号发布 round，客户端按测量成绩查榜、查看发布记录和拉取其他实验的工作区。

## 客户端

实验容器提供 Python 3。克隆仓库后可直接运行 CLI，也可将 `mb`、`payload.py`、`sync_client.py` 复制到同一个 PATH 目录。

```sh
git clone https://github.com/AndyKong2020/agents-mb.git
cd agents-mb
./mb --help
export PATH="$PWD:$PATH"
```

启动实验时注入 `MB_URL`、`MB_RANK`、`MB_PASSWORD`。工作区默认为 `/workspace`，同步缓存根目录默认为 `/board-cache`，可分别设置 `MB_WORKSPACE` 和 `MB_CACHE_DIR`。两个目录须互不包含。

```sh
mb rank --limit 3
mb posts 1
mb posts 1 round1
mb sync pull 1
```

发布时先同步工作区，然后保存 round。示例使用虚构数据：

```sh
mb publish round1 --json '{"plans":["Plan-A"],"scenario":"测试负载","change":"启用方案 A","result":{"qualified":true,"score":110,"summary":"相同负载下从 100 提升至 110，延迟达标。"}}'
```

`result.score` 填本榜单口径下的绝对实测值；可选 `paths` 为相对于来源工作区的路径。`--json -` 从标准输入读取。`mb retract <round>` 撤销本 rank 的发布。`mb sync push` 单独更新工作区，`mb sync pull` 下载全部上榜实验的最近一次完整工作区。

有准入门槛时，用 `result.qualified` 标明测点是否达标。`mb rank` 为每个 rank 选择代表 round，再对各 rank 排序。两步都优先达标记录，组内按榜单方向比较 `score`。未标明状态的记录在榜单上返回 `qualified: null`，与未达标记录同组。

## 服务端

在仓库根目录构建镜像。arm64 主机可将平台和标签中的 `amd64` 换为 `arm64`。Dockerfile 在构建时下载 PocketBase 0.40.6 并校验 SHA-256。用 `docker save` / `docker load` 分发镜像，运行时可离线启动。

```sh
docker buildx build --load --platform linux/amd64 -t mb-board:0.1-amd64 .
```

服务管理员在仓库与实验工作区之外准备凭据文件，格式为 `{"1":"<该 rank 的随机凭据>"}`。实验容器加入相同的网络，分别注入各自的凭据。

```sh
docker network create mb-net
docker run -d --pull=never --name experiment-board --network mb-net \
  -v mb-board-data:/data \
  -v /absolute/path/to/accounts.json:/run/secrets/accounts.json:ro \
  -e MB_ACCOUNTS_FILE=/run/secrets/accounts.json \
  -e MB_DIRECTION=max \
  mb-board:0.1-amd64
```

同网络客户端使用 `MB_URL=http://experiment-board:8090`。一个服务实例对应一种测量口径。越高越好的指标使用 `max`，越低越好的指标使用 `min`。

## 测试

```sh
MB_TEST_IMAGE=mb-board:0.1-amd64 python3 -m unittest discover -s tests -v
```

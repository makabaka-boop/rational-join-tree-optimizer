# joinplan

无数据库依赖的 JSON 连接顺序规划器（纯 Python 标准库）。给定 2～9 张表、
表间谓词及其有理选择率，枚举所有合法二叉连接树，返回总代价最小的树。

## 模型

- 允许任意二叉连接树，但每次合并的两侧之间必须至少有一条谓词。
- 子树估计行数 = 子树内所有基表行数之积 × 子树内部全部谓词选择率之积
  （精确有理数运算）。
- 总代价 = 所有非叶节点估计行数之和。
- 并列时：每个内部节点的左右子树按树串字节序排列，取全括号树串字典序
  最小者。
- 谓词图不连通时，返回各连通分量（各自给出最优子计划）。

## 输入（stdin 或文件参数）

```json
{
  "tables": [{"name": "A", "rows": 100}, {"name": "B", "rows": 200}],
  "predicates": [{"left": "A", "right": "B", "selectivity": "1/10"}]
}
```

- `tables`：2～9 张，表名为唯一可打印 ASCII（不含括号），`rows` 为正整数。
- `predicates`：可省略；`left`/`right` 必须是不同的已声明表；同一表对
  可有多条谓词。`selectivity` 为 `[0,1]` 内的有理数，写作 `"p/q"`
  （也接受整数 `0`/`1`；未约分的分数会自动约分）。

## 输出

连通：

```json
{"status": "ok", "cost": "27000/1", "tree_string": "((AB)C)", "tree": {...}}
```

不连通：

```json
{"status": "disconnected", "components": [{"tables": [...], "cost": "p/q",
  "tree_string": "...", "tree": {...}}, ...]}
```

输入非法时输出 `{"status": "error", "error": "..."}` 并以退出码 2 结束。

树节点：叶子为 `{"type": "table", "name": ..., "rows": <int>}`；连接节点为
`{"type": "join", "rows": "p/q", "tables": [...], "predicates": [...],
"children": [left, right]}`，其中 `predicates` 是在该次合并首次生效的谓词
（按输入顺序），`children` 按子树串字节序排列。所有有理数（`cost`、连接
节点 `rows`）以约分后的 `"p/q"` 字符串表示。

## 运行

本地：

```sh
python3 joinplan.py < examples/chain3.json
python3 joinplan.py examples/bushy4.json
```

Compose（`joinplan` 服务）：

```sh
docker compose build joinplan
docker compose run -T joinplan < examples/chain3.json
```

## 测试

pytest 对小图枚举所有合法二叉树（不做子集剪枝），与规划器对拍总代价和
并列裁决，并逐节点校验估计行数、首次生效谓词、子节点顺序与合并合法性：

```sh
python3 -m pytest -q                      # 本地
docker compose run --rm joinplan-tests    # 或经 Compose
```

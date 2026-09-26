# joinplan：无数据库依赖的 JSON 多表连接规划器

`joinplan` 使用 Python 标准库实现，不依赖数据库或第三方运行库。规划器枚举所有连通子问题及所有合法二叉划分，得到全局最小总代价；不是只挑当前最小的两张表。

## 输入

通过 `POST /plan` 或标准行输入一个 JSON 对象：

```json
{
  "tables": {
    "a": 100,
    "b": 20,
    "c": 50
  },
  "predicates": [
    {"id": "p_ab", "left": "a", "right": "b", "selectivity": {"numerator": 1, "denominator": 4}},
    {"id": "p_bc", "left": "b", "right": "c", "selectivity": "2/10"}
  ]
}
```

约束：

- 表数为 2～9；表名为非空、唯一 ASCII 字符串。
- 行数为正整数。
- 谓词连接两张不同表；同一表对可有多条谓词。
- 谓词 `id` 必须是非空、唯一 ASCII 字符串。
- `selectivity` 是 0～1 的精确有理数，支持：
  - `{"numerator": 1, "denominator": 3}`
  - `[1, 3]`
  - `"1/3"`
  - 整数 `0` 或 `1`
  - 十进制小数（如 `0.25`，服务端按精确定点值转换）
- 输出中的分数均为约分形式。

## 代价模型

对任意子树：

```text
估计行数 = 子树内全部基表行数之积 × 子树内部全部谓词选择率之积
总代价 = 每个非叶节点估计行数之和
```

每次二叉合并的两侧之间必须至少有一条谓词。某谓词在第一次同时包含其左右表的节点生效；同一表对的多条谓词会在同一个节点一起生效，并按谓词 ID 的 ASCII 字节序列出。

## 并列裁决

每个内部节点先把左右子树按树串字节序排列。总代价相同的方案，取最终全括号树串字典序最小者。叶子使用 JSON 字符串表示，因此任意 ASCII 表名都不会与括号、逗号分隔符冲突，例如：

```text
("a",("b","c"))
```

## 成功输出

```json
{
  "connected": true,
  "cost": {"numerator": 100, "denominator": 1},
  "tree": "(\"a\",\"b\")",
  "root": {
    "type": "join",
    "tree": "(\"a\",\"b\")",
    "left": {
      "type": "leaf",
      "table": "a",
      "tree": "\"a\"",
      "estimated_rows": {"numerator": 10, "denominator": 1}
    },
    "right": {
      "type": "leaf",
      "table": "b",
      "tree": "\"b\"",
      "estimated_rows": {"numerator": 20, "denominator": 1}
    },
    "effective_predicates": ["p_ab"],
    "estimated_rows": {"numerator": 500, "denominator": 1}
  }
}
```

## 无法连通

如果谓词图不能连成一棵树，不猜测笛卡尔积；返回连通分量：

```json
{
  "connected": false,
  "components": [
    {"tables": ["a", "b"]},
    {"tables": ["c"]}
  ]
}
```

## 本地运行

不需要安装运行时依赖：

```bash
python -m joinplan --stdin < request.json
```

启动 HTTP 服务：

```bash
python -m joinplan --host 0.0.0.0 --port 8000
```

调用：

```bash
curl -s http://127.0.0.1:8000/plan \
  -H 'Content-Type: application/json' \
  --data '{"tables":{"a":10,"b":2},"predicates":[{"id":"p","left":"a","right":"b","selectivity":"1/2"}]}'
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/health
```

## Compose

```bash
docker compose up --build
```

服务名为 `joinplan`，宿主机端口为 `8000`。

## 测试

测试使用 pytest：

```bash
python -m pytest -q
```

测试会对 2、3、4 张表枚举所有可能谓词图，并独立枚举所有合法/非法全括号二叉树，对拍：

- 每个内部节点的有理估计行数；
- 总代价；
- 谓词首次生效节点；
- 多谓词选择率连乘；
- 代价并列时的字节序/字典序裁决；
- 不连通图的连通分量。

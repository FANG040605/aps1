# 宏达精密 · 本地排产助手（简化版 APS）

一句话：把计划员的 Excel 台账（订单 / 设备 / 工艺路线）变成一张车间照着能干的周排单表，并告诉他哪几单要赶不上。

## 一、做什么

给常州宏达精密的计划员用。拖入台账 → 点一下 → 出四件产物：

1. 周排单表（可打印：机台 / 单号 / 工序 / 数量 / 开工 / 完工 / 工时）
2. 甘特图（单文件 HTML，断网也能双击打开）
3. 交期风险清单（单号 / 交期 / 预计完工 / 差额 / 卡在哪台机）
4. 数据体检报告（脏数据分类、处理方式、需客户确认项）

纯本地运行、不联网、不写数据库，客户数据不出本机。

**做**：作业车间排产、总完工最短单目标、同组机台任选与停机替代、交期风险清单、单文件本地运行。
**不做**：柔性工艺路线优化、多目标权衡、换型时间与人员资质、插单自动重排、多用户与服务器部署。

## 二、怎么跑

### 环境

| 项 | 版本 | 检查命令 |
|---|---|---|
| Python | 3.10+（本项目在 3.13.14 开发验证） | `python --version` |
| openpyxl | 3.1.5 | `python -c "import openpyxl; print(openpyxl.__version__)"` |
| Git | 2.x（仅开发需要，运行不需要） | `git --version` |

装依赖（只需这一个第三方库）：

```bash
pip install openpyxl
```

### 命令行跑一次（后端）

```bash
python run.py data/宏达精密_订单台账.xlsx --out out
```

产物落在 `out/`：

```
out/排单表.xlsx    out/甘特图.html    out/风险清单.md    out/体检报告.md
```

### 网页版（前后端打通后）

```bash
python web/app.py --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

### 单元测试

```bash
python -m unittest discover -s tests -v
```

> 说明：`run.py` 已可用（N4 完成）；`web/app.py` 在 N5 落地。
> 产物目录 `out/` 已加入 .gitignore——里面是客户数据的衍生结果，不进版本库。

## 三、目录说明

```
aps-scheduler/
├── README.md              本文件：做什么、怎么跑、目录
├── .gitignore             排除缓存 / 产物 / 客户数据（红线）
├── config.py              长期参数：班次、日历、交期口径、工时单位
├── run.py                 命令行入口（N4）
├── core/                  后端模块，一个文件一件事
│   ├── reader.py          读三个 sheet（跳过标题行与注释行）
│   ├── cleaner.py         洗脏数据 + 逐条记 issues
│   ├── instance.py        订单 → 工序任务（分钟/件 × 数量）
│   ├── solver.py          规则排程（FIFO / SPT / MWKR 取最好）
│   ├── checker.py         四条硬约束，独立于求解器
│   └── report.py          排单表 / 甘特 / 风险 / 体检
├── web/                   本地服务与前端
│   ├── app.py             http.server，标准库
│   └── index.html         单页：上传 → 生成 → 四页签 → 下载
├── tests/                 单元测试（≥3 个）
├── docs/
│   ├── 设计说明书.md      N2
│   ├── ui/                N3：三张图 + 交互说明
│   └── 测试报告.md        N6
├── data/                  客户台账放这里，已 gitignore，绝不进库
└── out/                   程序产物，已 gitignore
```

### 分支与提交约定

- `main`：随时可跑的版本；`dev`：日常开发。
- 提交信息格式 `feat: / fix: / docs: / refactor: / test: ` + 一句话，说清改了什么。

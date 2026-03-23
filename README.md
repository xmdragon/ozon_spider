# Ozon Spider

抓取 [ozon.ru](https://www.ozon.ru/) 商品详情数据，绕过反爬检测，输出结构化 JSON。

## 功能

- 通过 CDP 接管系统 Chrome（headful 模式，Xvfb 虚拟显示）
- 并行请求 Page1 + Page2 API，提取完整商品数据
- 自动处理滑块验证码
- 自修复循环：连续成功 3 次后退出
- 增量保存 cookies，提升会话稳定性

## 输出字段

每个商品包含：

| 字段 | 说明 |
|------|------|
| `sku` | 商品 SKU |
| `name` | 商品名称 |
| `price` | 当前价格 |
| `cardPrice` | 卡片价格（Ozon 卡专属） |
| `realPrice` | 实际到手价（按扩展公式计算） |
| `original_price` | 划线原价 |
| `images` | 图片列表 |
| `attributes` | 商品属性列表 |
| `typeNameRu` | 商品类型（俄文） |
| `description` | 商品描述 |

## 依赖

```bash
pip install playwright requests numpy pillow scipy
playwright install chromium
sudo apt install google-chrome-stable xvfb
```

## 配置

编辑 `config.py`：

- `SKUS`：要抓取的 SKU 列表
- `CHROME_BIN`：Chrome 可执行文件路径（默认 `/usr/bin/google-chrome-stable`）
- `CDP_PORT`：CDP 调试端口（默认 `9223`）
- `SUCCESS_THRESHOLD`：连续成功次数阈值（默认 `3`）

## 运行

```bash
python3 run.py
# 或
bash start.sh
```

结果保存至 `results.json`。

## 文件结构

```
ozon_spider/
├── config.py          # SKU 列表、超时、路径配置
├── run.py             # 入口：Xvfb + Chrome 生命周期，自修复循环
├── spider.py          # 主逻辑：CDP 接管、商品页抓取、稳定页判断
├── extractor.py       # 数据提取：widgetStates 解析、页面状态分类
├── chrome_launcher.py # 启动系统 Chrome + Xvfb
├── slider_solver.py   # 滑块验证码求解（模板匹配 + 抛物线拟合）
└── start.sh           # 快捷启动脚本
```

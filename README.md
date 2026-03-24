# Ozon Spider API

抓取 [ozon.ru](https://www.ozon.ru/) 商品详情，并通过 seller 会话补充尺寸重量，统一对外提供 HTTP API。

## 当前架构

- 匿名 spider：
  使用系统 Chrome + Xvfb 抓取商品页和 Page1/Page2 API
- seller session：
  使用独立 Chrome profile 维持 Ozon Seller 登录态
- 统一服务：
  由 [server.py](/home/grom/ozon_spider/server.py) 启动 FastAPI，对外提供接口

## API

### `GET /health`

返回服务状态，包括：
- spider worker pool 状态
- seller active/standby 状态

### `GET /sku?sku=<SKU>`

返回单个 SKU 的完整商品数据：
- 基础信息
- 图片
- 属性
- seller 尺寸重量

示例：

```bash
curl "http://127.0.0.1:8765/sku?sku=3714928277"
```

### `POST /variant-model`

批量查询 seller 尺寸重量。

请求：

```json
{"skus":["2036172405","3714928277"]}
```

### `POST /data-v3`

批量查询 seller `data/v3` 数据。

请求：

```json
{"skus":["2036172405","3714928277"]}
```

## 运行

### 依赖

```bash
pip install playwright requests numpy pillow scipy fastapi uvicorn
playwright install chromium
sudo apt install google-chrome-stable xvfb
```

### 本地启动

```bash
python3 -m uvicorn server:app --host 127.0.0.1 --port 8765
```

服务启动后访问：

```bash
curl http://127.0.0.1:8765/health
```

## 配置

项目从 [`.env`](/home/grom/ozon_spider/.env) 和 [config.py](/home/grom/ozon_spider/config.py) 读取配置。

当前关键配置：

- `CHROME_BIN`
  Chrome 可执行文件路径
- `XVFB_DISPLAY`
  虚拟显示器，默认 `:99`
- `SELLER_ACCOUNTS`
  seller 账号列表，格式：
  `email:app_password:client_id,email2:app_password2:client_id2`

示例：

```env
SELLER_ACCOUNTS=50713906@qq.com:app_password:3465475,xmdragon0808@163.com:app_password:3092234
```

## 邮箱验证码

seller 登录当前只支持这些邮箱类型：

- `qq.com`
- `163.com`
- `126.com`

相关逻辑在 [email_service.py](/home/grom/ozon_spider/email_service.py)。

## 主要文件

- [server.py](/home/grom/ozon_spider/server.py)
  FastAPI 服务入口
- [spider.py](/home/grom/ozon_spider/spider.py)
  匿名商品抓取主逻辑
- [spider_pool.py](/home/grom/ozon_spider/spider_pool.py)
  匿名 spider worker pool
- [extractor.py](/home/grom/ozon_spider/extractor.py)
  商品数据提取和页面状态分类
- [seller_login.py](/home/grom/ozon_spider/seller_login.py)
  seller 会话管理、主备切换、登录恢复
- [email_service.py](/home/grom/ozon_spider/email_service.py)
  QQ / 163 IMAP 验证码收取
- [chrome_launcher.py](/home/grom/ozon_spider/chrome_launcher.py)
  Chrome / Xvfb 启动封装

## 说明

- 价格字段当前直接返回页面抓到的实时值
- `GET /sku` 对 seller 尺寸重量是强依赖，seller 不可用时会返回失败
- 运行时产生的浏览器 profile、cookies、seller state 不建议放在仓库目录内

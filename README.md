# XHS URL Cleaner to Feishu Bitable

一个可部署到 Render 的 FastAPI + Docker 服务：接收小红书分享链接，去掉 `xsec_token` 等所有 query 参数，只保留网页本体链接，然后新增到飞书多维表格。

## 功能

- `GET /`：内置网页前端，可粘贴链接并提交。
- `GET /health`：健康检查。
- `POST /clean`：只清洗链接，不写飞书。
- `POST /submit`：清洗链接并写入飞书多维表格。
- `POST /submit-batch`：批量清洗并写入飞书。
- `POST /extract-note`：用无头 Playwright 移动端伪装提取小红书笔记标题、正文、标签、图片 URL。
- `POST /extract-note-submit`：提取笔记后创建飞书多维表格记录，并可把图片上传到附件字段。

清洗规则：保留 `scheme + host + path`，删除 query 和 fragment。

示例：

```text
https://www.xiaohongshu.com/goods-detail/688c1495b1b9df00012f1a34?xsec_token=xxx&share_id=xxx
```

变为：

```text
https://www.xiaohongshu.com/goods-detail/688c1495b1b9df00012f1a34
```

## 环境变量

复制 `.env.example` 为 `.env` 并填写：

```env
SERVICE_API_KEY=change_me_to_a_random_string
FEISHU_APP_ID=cli_xxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxx
FEISHU_BITABLE_APP_TOKEN=base_xxxxxxxxxxxxx
FEISHU_TABLE_ID=tblxxxxxxxxxxxxx
FEISHU_FIELD_NAME=小红书链接
FEISHU_FIELD_TYPE=text

# Feishu Bitable target for extracted note materials
FEISHU_NOTES_BITABLE_APP_TOKEN=base_xxxxxxxxxxxxx
FEISHU_NOTES_TABLE_ID=tblxxxxxxxxxxxxx
FEISHU_NOTES_TITLE_FIELD=标题
FEISHU_NOTES_BODY_FIELD=正文
FEISHU_NOTES_TAGS_FIELD=标签
FEISHU_NOTES_IMAGE_FIELD=图片
FEISHU_NOTES_FETCHED_FIELD=是否拉取
FEISHU_NOTES_CATEGORY_FIELD=笔记类别
FEISHU_NOTES_FETCHED_YES_VALUE=是
```

可选：

```env
FEISHU_ORIGINAL_URL_FIELD=原始链接
FEISHU_XHS_ID_FIELD=小红书ID
FEISHU_SUBMITTED_AT_FIELD=提交时间
```

> 不要提交 `.env` 到 GitHub。

## 本地运行

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
uvicorn main:app --host 0.0.0.0 --port 8000
```

打开：

```text
http://localhost:8000/
```

## Docker 构建

```bash
docker build -t xhs-feishu-service .
```

## Docker 运行

```bash
docker run --rm -p 8000:8000 --env-file .env xhs-feishu-service
```

## API 测试

只清洗：

```bash
curl -X POST "http://localhost:8000/clean" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change_me_to_a_random_string" \
  -d '{"url":"https://www.xiaohongshu.com/goods-detail/abc?xsec_token=xxx&share_id=1"}'
```

写入飞书：

```bash
curl -X POST "http://localhost:8000/submit" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change_me_to_a_random_string" \
  -d '{"url":"https://www.xiaohongshu.com/goods-detail/abc?xsec_token=xxx&share_id=1"}'
```


提取笔记（只返回数据，不写飞书）：

```bash
curl -X POST "http://localhost:8000/extract-note" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change_me_to_a_random_string" \
  -d '{"url":"26下教资综合素质 http://xhslink.com/o/6HBt0NzJpaO 复制文本后进入【小红书】"}'
```

响应形状：

```json
{
  "ok": true,
  "note": {
    "success": true,
    "reason": "",
    "input_url": "http://xhslink.com/o/6HBt0NzJpaO",
    "final_url": "https://www.xiaohongshu.com/...",
    "note_id": "...",
    "title": "...",
    "description": "...",
    "tags": ["教资"],
    "tags_text": "#教资",
    "image_urls": ["https://..."],
    "images": []
  }
}
```

提取并写入飞书：

```bash
curl -X POST "http://localhost:8000/extract-note-submit" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change_me_to_a_random_string" \
  -d '{"url":"http://xhslink.com/o/6HBt0NzJpaO","category":"教资","upload_images":true,"max_images":18}'
```

注意：保留原始短链或带 `xsec_token` 的完整分享链接；裸 `discovery/item/{note_id}` 链接无参数时容易进入安全/登录页。

## Render 部署

1. 将本目录推送到 GitHub 仓库。
2. Render 新建 `Web Service`。
3. 选择 `Docker` 环境。
4. 指向包含 `Dockerfile` 的仓库目录。
5. 在 Render Environment Variables 中填写 `.env.example` 中的变量。
6. Health Check Path 设置为 `/health`。
7. 部署后访问 `https://你的服务.onrender.com/` 使用前端。

## 飞书准备

1. 创建飞书自建应用，获取 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。
2. 开通多维表格新增记录相关权限。
3. 确保应用有目标多维表格访问权限。
4. 获取 `FEISHU_BITABLE_APP_TOKEN`、`FEISHU_TABLE_ID`。
5. 确认 `FEISHU_FIELD_NAME` 字段存在。若不确定字段类型，先用普通文本字段并设置 `FEISHU_FIELD_TYPE=text`。


## 本次本地验收结果

- Python 语法检查：`main.py`、`xhs_extractor.py`、`test_app.py` 均通过 `py_compile`。
- 单元测试：`python -m pytest -q` 通过，`16 passed`；覆盖清洗、登录、飞书字段、提取接口响应形状和边界输入。
- 真实无登录提取烟测：短链 `http://xhslink.com/o/6HBt0NzJpaO` 可提取标题、正文、10 个标签、15 张图片链接。
- 本地 API 烟测：`POST /extract-note` 返回 `200`，响应形状为 `{"ok": true, "note": ...}`。
- Docker CLI：已安装，版本 `29.1.3`。
- Docker 镜像构建：当前机器 Docker Desktop 服务端返回 `500 Internal Server Error`，`docker version/info/build` 均无法连接 Server，因此镜像构建需在 Docker Desktop 恢复后执行，或直接交给 Render/GitHub Actions 构建。

如需本机继续构建，先重启 Docker Desktop，确认：

```bash
docker info
```

能正常显示 Server 信息后，再执行：

```bash
cd xhs_feishu_service
docker build -t xhs-feishu-service:local .
```

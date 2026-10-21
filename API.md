# JSON API 说明与测试

本项目同时提供网页和 `/api/v1` JSON API。两者使用同一个 MongoDB、题目校验器、条件跳转器和统计计算器。所有响应均为 JSON；成功响应和错误响应使用标准 HTTP 状态码。题目 ID 和问卷 ID 均为字符串。

## 身份验证与请求格式

API 使用与网页相同的登录会话 Cookie。先 `GET /api/v1/csrf`，保存响应 Cookie 和 `csrf_token`。所有 `POST`、`PATCH`、`DELETE` 请求都须发送 `Content-Type: application/json` 和 `X-CSRF-Token: <令牌>`。登录会更新会话，请改用登录响应中的新 `csrf_token`。不允许跨域调用；其他设备可直接访问同一个服务地址。

未登录访问私人资源返回 401；他人的问卷或答卷返回 404；数据校验失败返回 422；状态、版本或重复填写冲突返回 409；CSRF 错误返回 400。错误响应形如 `{"error":"原因"}`，题目答案校验错误还包含 `question_id`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/csrf` | 获取 CSRF 令牌和会话 Cookie |
| POST | `/api/v1/auth/register` | 注册：`username`、`password`、`confirmation` |
| POST | `/api/v1/auth/login` | 登录：`username`、`password`；返回新 CSRF 令牌 |
| GET | `/api/v1/auth/me` | 查看当前账号 |
| POST | `/api/v1/auth/logout` | 退出 |
| GET / POST | `/api/v1/surveys` | 列出自己的问卷 / 创建问卷 |
| GET / PATCH / DELETE | `/api/v1/surveys/<问卷ID>` | 查看、修改、删除自己的问卷 |
| POST | `/api/v1/surveys/<问卷ID>/publish` | 发布草稿 |
| POST | `/api/v1/surveys/<问卷ID>/close` | 关闭已发布问卷 |
| GET / POST | `/api/v1/surveys/<问卷ID>/questions` | 列出、添加题目 |
| GET / PATCH / DELETE | `/api/v1/surveys/<问卷ID>/questions/<题目ID>` | 查看、修改、移除题目 |
| POST | `/api/v1/surveys/<问卷ID>/questions/<题目ID>/jumps` | 添加条件跳转 |
| PATCH / DELETE | `/api/v1/surveys/<问卷ID>/questions/<题目ID>/jumps/<规则ID>` | 上移、下移、删除规则 |
| GET | `/api/v1/surveys/<问卷ID>/statistics` | 整卷统计 |
| GET | `/api/v1/surveys/<问卷ID>/statistics/<题目ID>` | 单题统计 |
| GET | `/api/v1/public/surveys/<slug>` | 登录后读取已发布问卷 |
| POST | `/api/v1/public/surveys/<slug>/assess` | 校验当前答案，返回经过的题目及错误 |
| POST | `/api/v1/public/surveys/<slug>/responses` | 提交答卷 |
| GET | `/api/v1/responses` | 查看自己的答卷列表 |
| GET | `/api/v1/responses/<答卷ID>` | 查看自己提交的答卷 |

创建问卷的 JSON 字段为 `title`、`description`、`allow_anonymous`、`allow_multiple_submissions`、`option_label_style`（`numeric` 或 `letters`）和 `deadline`（可选，北京时间 `YYYY-MM-DDTHH:MM`）。创建返回 201、问卷 `id`、`slug`、`public_path`、`public_url`、`revision`。后续修改可带读取到的 `revision`；旧版本返回 409。问卷说明等字段可通过 PATCH 调整，截止时间设为 `null` 表示不设截止时间。

添加题目的 `type` 支持 `single_choice`、`multiple_choice`、`text`、`number`。`required` 是布尔值。选择题 `options` 是不重复的文字数组；多选可使用 `min_select` / `max_select`，或单独使用 `exact_select`；文本题可使用 `min_length` / `max_length`；数字题可使用 `min_value` / `max_value` / `integer_only`。数字题还可设置 `number_statistics`：`distribution`（默认，页面以答案及人数为主）或 `measure`（页面直接展示均值等）。两种模式的统计 API 都返回 `number_frequencies`、`average`、`median`、`minimum`、`maximum`；模式只影响页面的展示重点，不会改变已存答卷。题目约束与网页相同。

跳转规则的 JSON 包含后续题目的 `target_id`。单选题加 `option`；多选题加 `mode`（`any`、`all`、`exact`）及 `match_options` 数组；数字题加 `operator`（`lt`、`lte`、`eq`、`gte`、`gt`）及 `threshold`。规则按顺序匹配，第一条命中生效；PATCH 规则时用 `{"direction":"up"}` 或 `{"direction":"down"}` 调整顺序。

提交答卷时须发送从 GET 问卷取得的 `revision` 和以题目 ID 为键的 `answers`。单选、填空值为文字或数字；多选值为数组。允许匿名的问卷可传 `identity_mode: "anonymous"` 或 `"named"`。条件跳转跳过的题目不必回答，也不会保存。若填写期间问卷改变，返回 409，客户端应重新读取问卷。提交成功返回 201 和答卷 ID；已提交答卷不可修改。

## 调用示例

以下 Python 示例使用标准库与 CookieJar，适用于本机或能访问服务的其他设备；把 `BASE` 改为实际地址：

```python
import json
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, Request, build_opener

BASE = "http://127.0.0.1:5000"
client = build_opener(HTTPCookieProcessor(CookieJar()))

def call(method, path, data=None, token=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    if token:
        headers["X-CSRF-Token"] = token
    request = Request(BASE + path, data=body, headers=headers, method=method)
    with client.open(request) as response:
        return response.status, json.load(response)

_, csrf = call("GET", "/api/v1/csrf")
token = csrf["csrf_token"]
_, login = call("POST", "/api/v1/auth/login",
                {"username": "你的账号", "password": "你的密码"}, token)
token = login["csrf_token"]
status, surveys = call("GET", "/api/v1/surveys")
print(status, surveys)
```

## API 测试用例

运行 `.\.venv\Scripts\python.exe -m unittest tests.test_json_api tests.test_json_api_http -v`。测试使用独立的临时 MongoDB 数据库，结束时删除，不修改正式问卷。`test_json_api_http` 还会在随机本地端口启动服务，通过真实 HTTP、两个独立 CookieJar 测试请求与响应。以下是测试覆盖的主要步骤：

| 步骤 | 输入 | 预期输出 | 实际结果 |
| --- | --- | --- | --- |
| 注册、登录两个账号 | 不重复用户名、6 位密码、确认密码 | 201；登录返回会话和新 CSRF 令牌 | 通过 |
| 创建问卷 | 标题、匿名填写、仅填一次、字母选项编号 | 201；返回草稿和公开路径 | 通过 |
| 添加四类题目 | 单选、多选、文本、数字及限制 | 每题返回 201 和题目 ID | 通过 |
| 添加单选跳转 | 选“是”跳过第二题 | 返回规则；作答路径为第 1、3、4 题 | 通过 |
| 添加多选与数字跳转 | 多选包含指定选项；数字达到阈值 | 两种规则均跳到设定的后续题目 | 通过 |
| 检查多选限制 | 只选 1 项，要求选 2 项 | 422，包含出错题目 ID | 通过 |
| 提交匿名答卷 | 合法答案、问卷版本、匿名身份 | 201；数据库仅保存经过题目的答案 | 通过 |
| 允许重复填写 | 同一账号再次提交允许多次的问卷 | 新答卷 ID；统计总数增加 | 通过 |
| 查看统计 | 读取整卷、单选题和数字题统计 | 回答数、各选项人数和平均值正确 | 通过 |
| 权限与重复提交 | 他人读取问卷/答卷，重复填写 | 404 / 409；数据不泄露、不重复写入 | 通过 |
| CSRF、内容类型、旧版本 | 缺令牌、非 JSON、旧 `revision` | 400、415、409 | 通过 |
| 修改进行中的问卷 | 填写者读取后，创建者修改题目 | 旧 `revision` 提交返回 409，新版可提交 | 通过 |
| 编辑、关闭、删除 | 修改问卷、关闭后填写、删除 | 更新成功；关闭后 409；答卷随问卷删除 | 通过 |

网页版仍有独立的完整流程测试。运行 `.\.venv\Scripts\python.exe -m unittest discover -s tests -q` 可一起复查。

## 不在同一局域网时分享

本机 `127.0.0.1` 链接只能本机使用。不同网络可选：

1. **短期演示：临时公网隧道。** 在本机启动网页后运行 `cloudflared tunnel --url http://127.0.0.1:5000`，获取随机的 HTTPS `trycloudflare.com` 地址。把问卷的 `/survey/<slug>` 路径接在这个 HTTPS 地址后发给别人。若希望管理页的“复制链接”直接给出正确的 HTTPS 地址，先把 `.env` 的 `PUBLIC_BASE_URL` 设为该临时地址并重启网页服务；隧道地址变更时也须更新。无需自购域名；停止隧道后地址失效。本机、MongoDB 和网页服务须持续运行。它适合课程演示，不适合长期托管。
2. **长期可访问：带公网地址的服务器或托管平台。** 部署同一份 Flask 代码并配置 MongoDB、会话密钥和 `PUBLIC_BASE_URL`。可以使用平台提供的地址，不一定要购买域名；具体可用地址取决于平台。
3. **仅同一局域网：** 使用电脑的局域网 IP 加端口，确保 Windows 防火墙允许访问。当前电脑的局域网地址可能随网络变化，不应写死在代码里。

公网隧道是把本机服务向持有链接的人开放。演示时不要使用真实个人信息，结束后关闭隧道。项目当前使用 Flask 本地服务；正式长期对外运行应使用适当的部署方式。

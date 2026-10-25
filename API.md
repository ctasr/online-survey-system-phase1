# JSON API 说明与测试

本项目同时提供网页和 `/api/v1` JSON API。第二阶段在第一阶段接口基础上增加题目库、版本、共享、个人题库和跨问卷统计接口。两者使用同一个 MongoDB，以及同一套题目校验、条件跳转和统计代码。API 的响应均为 JSON，并用 HTTP 状态码区分成功与失败。题目 ID 和问卷 ID 均为字符串。

## 身份验证与请求格式

API 使用与网页相同的登录会话 Cookie。先 `GET /api/v1/csrf`，保存响应 Cookie 和 `csrf_token`。所有 `POST`、`PATCH`、`DELETE` 请求都须发送 `Content-Type: application/json` 和 `X-CSRF-Token: <令牌>`。登录会更新会话，请改用登录响应中的新 `csrf_token`。项目没有配置浏览器跨域访问；浏览器应从网站同源调用 API，其他设备可访问已部署的公网地址。

未登录访问私人资源返回 401；他人的问卷或答卷返回 404；数据校验失败返回 422；状态、版本或重复填写冲突返回 409；CSRF 错误（含非ASCII令牌）返回 400；写请求缺少JSON对象、传入数组或格式损坏返回415，无参数操作也需发送 `{}`。错误响应形如 `{"error":"原因"}`，题目答案校验错误还包含 `question_id`。

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
| GET / POST | `/api/v1/library/questions` | 列出可用题目版本 / 创建独立题目 |
| GET | `/api/v1/library/questions/<版本ID>` | 查看题目版本及可见的历史版本 |
| POST | `/api/v1/library/questions/<版本ID>/versions` | 基于指定版本创建新版本 |
| POST | `/api/v1/library/questions/<版本ID>/restore` | 从旧版内容生成最新版本 |
| PATCH | `/api/v1/library/questions/<版本ID>/sharing` | 共享或停止共享该版本 |
| GET | `/api/v1/library/questions/<版本ID>/statistics` | 此版本的跨问卷统计；`?scope=lineage` 增加各版本概览 |
| GET / POST | `/api/v1/library/banks` | 列出 / 创建自己的题库 |
| GET / PATCH / DELETE | `/api/v1/library/banks/<题库ID>` | 查看、改名或删除题库；删除不影响题目和问卷 |
| POST | `/api/v1/library/banks/<题库ID>/questions` | 将版本加入题库 |
| PATCH / DELETE | `/api/v1/library/banks/<题库ID>/questions/<版本ID>` | 将题库条目更新到指定或最新可用版本 / 从题库移除 |
| POST | `/api/v1/surveys/<问卷ID>/library-questions` | 向草稿问卷加入题库题目的独立快照 |
| POST | `/api/v1/surveys/<问卷ID>/questions/<题目ID>/save-to-library` | 将现有问卷题目保存为独立题目 |

创建问卷的 JSON 字段为 `title`、`description`、`allow_anonymous`、`allow_multiple_submissions`、`option_label_style`（`numeric` 或 `letters`）和 `deadline`（可选，北京时间 `YYYY-MM-DDTHH:MM`）。创建返回 201、问卷 `id`、`slug`、`public_path`、`public_url`、`revision`。未设置 `PUBLIC_BASE_URL` 时，`public_url` 为 `null`，`share_ready` 为 `false`；`public_path` 仍可用于本站内导航。后续修改可带读取到的 `revision`，旧版本返回 409。问卷说明等字段可通过 PATCH 调整，截止时间设为 `null` 表示不设截止时间。

字段格式和问卷状态检查通过后，PATCH中提交的设置与当前值相同时返回200，不改变revision、updated_at或其他字段，不会使已加载问卷的填写者失效；实际有变化时才更新版本。截止时间按北京时间输入、与已保存的UTC时间比较。

删除问卷会同时删除该问卷的正式答卷和所有账号的未提交暂存；不删除其他问卷的数据，也不删除独立题目库中的来源版本。

添加题目的 `type` 支持 `single_choice`、`multiple_choice`、`text`、`number`。`required` 是布尔值。选择题 `options` 是不重复的文字数组；多选可使用 `min_select` / `max_select`，或单独使用 `exact_select`；文本题可使用 `min_length` / `max_length`；数字题可使用 `min_value` / `max_value` / `integer_only`。数字题还可设置 `number_statistics`：`distribution`（默认，页面以答案及回答份数为主）或 `measure`（页面直接展示均值等）。两种模式的统计 API 都返回 `number_frequencies`、`average`、`median`、`minimum`、`maximum`；数值等价的写法（如 `30`、`30.0`、`3e1`）在分布中合并，`values` 仍保留原始填写内容；模式只影响页面的展示重点，不会改变已存答卷。题目约束与网页相同。

第二阶段的题目库创建及新版本请求沿用上述题目字段。题目版本返回 `id`、`root_id`、`parent_id`、`owner_id`、`version`、`shared` 和 `definition`。新版本内容与来源相同时返回 422，不创建重复版本；显式恢复操作仍创建新版本。版本一经创建不原位修改；新版本按同一 `root_id` 递增，`parent_id` 指向所依据的版本。`restore` 同样创建新版本，历史版和已经发布的问卷保持原样。共享请求为 `{"shared": true}` 或 `false`；共享以单个版本为单位，其他用户只能读取和使用仍共享的版本。个人题库创建或改名用 `{"name":"常用题"}`，加入条目用 `{"question_id":"版本ID"}`；题库更新版本不会自动更新问卷。题库条目的 PATCH 可传 `{"target_id":"确认过的版本ID"}`，只更新到该指定版本；省略时明确表示更新到最新可用版本。目标必须属于同一道题，且仍归本人所有或处于共享状态。旧版停止共享后，仍可将已经收录的条目替换为可用新版；并发变更会保留其他条目，冲突无法解决时返回 409，不假报成功。向草稿问卷加入版本也用 `question_id`，可同时提交已读取的 `revision`；版本不一致返回 409，成功返回新的问卷内题目 ID。发布后新增、修改、删除题目或跳转规则返回 409，问卷信息的允许修改范围不变。

题库加题、改名、移除操作在写入时再次检查题库是否存在；请求途中题库被删除时返回404和JSON错误，不返回成功。

保存现有问卷题目到题目库时，会核对读取时的问卷版本、状态和题目仍有效；操作期间如有其他页面修改或保存，返回409和原因，不给新内容关联旧来源，也不保留该失败操作临时创建的题目。确认当前内容后可以重试。

跨问卷统计以**同一个题目版本**为单位，读取所有引用该版本的问卷和已提交答卷，返回 `survey_count`、`answered_count`、`option_counts`、`values`、`values_scope`、`average`。`usages` 列出使用此版本的问卷：自己的问卷返回 `id`、`title`、`status` 和 `own:true`；他人的问卷仅返回稳定的匿名 `reference` 和 `own:false`，不开放标题或详情。数字题还返回 `median`、`minimum`、`maximum` 和 `number_distribution`，后者按等价数值归并（例如 `30` 与 `30.0`），列出数值、回答份数和比例。回答次数、选项计数及数字分布汇总所有问卷；`values_scope` 为 `own_surveys`，逐条填写内容 `values` 只来自当前用户自己创建的问卷。不同版本的选项和约束可能不同，`scope=lineage` 返回逐版本概览，不把答案强行混算。向同一份草稿重复加入同一题目版本返回 409。

跳转规则的 JSON 包含后续题目的 `target_id`。单选题加 `option`；多选题加 `mode`（`any`、`all`、`exact`）及 `match_options` 数组；数字题加 `operator`（`lt`、`lte`、`eq`、`gte`、`gt`）及 `threshold`。规则按顺序匹配，第一条命中生效；PATCH 规则时用 `{"direction":"up"}` 或 `{"direction":"down"}` 调整顺序。

提交答卷时须发送从 GET 问卷取得的 `revision` 和以题目 ID 为键的 `answers`。单选、文本和数字填空值为单个文字或数字，不能传数组；多选值为数组，不能传单个值。格式不符时评估及提交接口均返回422，不创建答卷。允许匿名的问卷可传 `identity_mode: "anonymous"` 或 `"named"`。条件跳转跳过的题目不必回答，也不会保存。若填写期间问卷改变，返回 409，客户端应重新读取问卷。提交成功返回 201 和答卷 ID，并清除当前账号在本问卷的暂存；不影响其他账号的暂存。校验失败或问卷在提交期间关闭、截止时返回422或409，保留原暂存；已提交答卷不可修改。

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

运行 `.\.venv\Scripts\python.exe -m unittest tests.test_json_api tests.test_json_api_http tests.test_api_user_journeys tests.test_csrf_boundaries -v`。测试使用独立的临时 MongoDB 数据库，结束时删除，不修改正式问卷。`test_json_api_http` 还会在随机本地端口启动服务，通过真实 HTTP 和两个独立 CookieJar 测试请求与响应。下表列出主要验证点；逐项输入、实际输出和运行记录见[测试用例与结果](docs/第二阶段_测试用例与结果.md)。

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
| 发布后尝试改题 | 问卷已发布，创建者 PATCH 最后一道题 | 改题返回409，原revision不变，填写者仍可按原题201提交 | 通过 |
| 编辑、关闭、删除 | 修改问卷、关闭后填写、删除 | 更新成功；关闭后 409；答卷随问卷删除 | 通过 |

第二阶段的自动测试运行 `.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_stage2_library.py -q`，覆盖两个用户、两个问卷复用同一版本、共享权限、版本新建与恢复、发布后不可修改、题库改名和更新、跨问卷统计以及第一阶段旧问卷题目入库。完整套件运行 `python -m unittest discover -s tests -q`。

网页版仍有独立的完整流程测试。运行 `.\.venv\Scripts\python.exe -m unittest discover -s tests -q` 可一起复查。

## 网页暂存所用 JSON 端点

除 /api/v1 外，网页填写使用 `GET /survey/<slug>/draft` 读取、`POST` 保存、`DELETE` 清空暂存；同样需要登录，写入同样检查 X-CSRF-Token。这些是内部填写进度端点，不能用它们代替正式提交。

保存字段包括 `revision`、`identity_mode`、`answers`；此处 answers 使用网页字段名 `q_<题目ID>`，多选为数组，其他为文本。与正式 /api/v1 提交中直接使用题目ID的键名不同。暂存只恢复输入，不代表答案已经通过正式校验，也不计入统计。超过容量返回413，错误格式返回400并保留旧暂存；客户端恢复失败先停止覆盖，重试成功后再合并输入。容量上限是HTTP请求体100000字节，适用于暂存负载而非对单个题目另加业务字数限制。

## 最终 API 验证证据

实际路由快照中共有32条 /api/v1 路由规则，含一条规则上的不同HTTP方法，不以页面数量代替接口数量。完整套件含 test_json_api.py 4项、test_json_api_http.py 1项、test_api_user_journeys.py 7项及test_csrf_boundaries.py 2项；每项内包含完整流程和多个响应断言，另外第二阶段题库专项用例也直接请求JSON接口。测试的步骤与输入在测试源码及测试文档中，结果列在测试文档中，运行脚本可重新生成逐项记录。/api/v1 错误返回JSON；普通网页路由不作相同格式保证。

暂存POST的请求体必须为JSON对象，answers必须显式提供且为对象；显式 `{"answers": {}}` 表示清空暂存答案。空数组、null、缺少answers或损坏JSON返回400，并保留原稿。


## 网页统计明细的筛选与返回

答卷网页 `/responses/<答卷ID>` 和 `/surveys/<问卷ID>/responses/<答卷ID>` 支持 `options=all` 显示全部选项，默认突出已选答案。翻页链接及页码表单保留此显示方式；该参数不改变返回列表的筛选，不属于JSON API，也不修改已提交内容。

`GET /surveys/<问卷ID>/statistics/<题目ID>/accounts` 是创建者网页，不属于JSON API。文本答案精确筛选使用 `answer_key`（原文的64字符SHA-256摘要）和题目 `version`，不把完整长答案放入URL。服务端仍检查问卷所有权，只列出该问卷、该题目版本中匹配原文的账号答卷。未知摘要返回空列表，非法格式返回404，匿名身份不公开。

`stats_view`、`stats_q`、`stats_page`、`stats_hpage` 只用于恢复统计总览位置，与账号列表自身的 `q`、`page` 及答卷详情题目分页分开。原JSON提交与统计接口的格式、数据库答案和权限不变。

网页问卷列表和详情均按PUBLIC_BASE_URL生成公开填写地址。未配置时列表只提供本机填写入口，不显示复制分享按钮；浏览器拒绝自动复制时显示并选中只读链接供手动复制。这是网页交互，不新增JSON API，也不表示当前服务已部署公网。


单题统计网页的历史版本分页：`GET /surveys/<问卷ID>/statistics/<题目ID>` 可传`version`与`answer_page`，只改变所选版本的原始回答页；其他版本保持首页。旧版链接带版本锚点并自动展开，查看账号回答后返回同一版本。未知版本404，仍需问卷所有者登录。这些参数属于网页导航，JSON API及正式答案格式不变。


### 公共错误页与返回

网页HTTP异常统一使用中文提示和系统导航。有同站来源时可以返回上一页继续操作，无同站来源时回首页；不自动重发失败的写请求。API继续返回JSON，保留原状态码及Allow等响应头。


问卷列表按创建时间倒序，个人答卷列表按提交时间倒序；时间相同时按ID倒序，网页与JSON使用相同次排序。不改变返回字段或身份验证方式。


### 截止时间的校验和保存

截止设置的错误留在展开面板中，保留日期和时间；旧表单及最终写入冲突不覆盖另一页面修改，页面保留输入并带上最新revision，供用户确认后重试。问卷关闭后拒绝旧页面的截止修改。过期截止时间未修改内容的保存视为无改动，不增加revision或updated_at；新建或真正修改时仍要求未来时间。网页和JSON保持相同规则。


### 旧页面保存和同时修改

发布、关闭和删除核对表单revision及状态，并在最终写入时再次匹配；冲突返回409并展示最新内容，重复已完成操作不重写时间。删题、删除或调整跳转规则也核对旧表单；无revision的第一阶段旧文档使用与服务器一致的默认版本。跳转校验错误返回422，保留条件和目标题目；发生冲突时保留仍可编辑的输入，供用户确认后重试。题库改名比对原名，冲突保留拟定名称，写入未匹配时返回失败。JSON删除同样核对最终状态，题库改名最终冲突返回409。

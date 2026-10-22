# MongoDB schema（第一阶段）

本文件描述程序实际写入的文档结构。MongoDB 采用灵活文档结构；此处是应用层 schema，字段合法性由 `app/questions.py`、`app/surveys.py`、`app/api.py` 校验，索引由 `app/db.py` 在启动时建立。示例中的 `ObjectId(...)` 和 `ISODate(...)` 表示 BSON 类型，不是可直接粘贴的 JSON 字符串。

## users

```javascript
{
  _id: ObjectId("..."),
  username: "alice",
  password_hash: "scrypt:...", // 密码散列，不保存明文
  created_at: ISODate("2026-10-21T12:00:00Z")
}
```

`username` 建唯一索引。注册和登录只按账号查找；会话保存在浏览器签名 Cookie 中。

## surveys

```javascript
{
  _id: ObjectId("..."),
  owner_id: ObjectId("..."), // users._id
  slug: "Ab12Cd34",          // 公开链接 /survey/<slug>
  title: "示例问卷",
  description: "问卷说明",
  status: "draft",          // draft / published / closed
  option_label_style: "letters", // letters / numeric；只影响选项编号
  allow_anonymous: false,
  allow_multiple_submissions: true,
  repeat_policy_epoch: 0,
  deadline: null,           // 设置后为 UTC 日期
  revision: 1,
  created_at: ISODate("2026-10-21T12:00:00Z"),
  updated_at: ISODate("2026-10-21T12:00:00Z"),
  questions: [{
    id: "a1b2c3d4e5f6a7b8", // 稳定题目 ID
    version: 1,
    type: "single_choice",  // single_choice / multiple_choice / text / number
    title: "请选择一项",
    required: true,
    options: ["是", "否"],
    jumps: [{id: "rule-id", kind: "single_choice", option: "否", target_id: "后续题目ID"}]
  }],
  question_history: []      // 改题后保存旧版本，供历史答卷统计
}
```

`slug` 唯一，`owner_id` 建查询索引。发布时增加 `published_at`，关闭时增加 `closed_at`，均与 `created_at` 分开。多选题另有 `min_select`、`max_select`、`exact_select`；文本题有 `min_length`、`max_length`；数字题有 `min_value`、`max_value`、`integer_only`、`number_statistics`。跳转规则依题型使用选项、选项集合或数字比较条件，并仅指向后续题目。未设置的可选字段可能不存在或为 `null`；详细校验见 [系统与数据库设计](系统与数据库设计.md)。

## responses

```javascript
{
  _id: ObjectId("..."),
  survey_id: ObjectId("..."),   // surveys._id
  survey_title: "示例问卷",      // 提交当时的标题
  respondent_id: ObjectId("..."), // 具名提交；匿名时为 null
  viewer_key: "不可逆派生值",   // 供填写者找回本人答卷
  answers: [{question_id: "a1b2c3d4e5f6a7b8", question_version: 1, value: "是"}],
  submitted_at: ISODate("2026-10-21T12:05:00Z")
}
```

`survey_id`、`(viewer_key, submitted_at)`、`(respondent_id, submitted_at)` 建查询索引。仅限制一次提交时，额外写入 `repeat_limited: true` 和不可逆的 `respondent_key`，并对 `(survey_id, respondent_key)` 建局部唯一索引。匿名答卷不存明文账号 ID。`answers` 只保存实际经过的题目：已回答的 `value` 按题型为文字、数字文本或选项数组；经过但未答的选答题保存 `null`，以便区分选答留空和条件跳过。被跳过的题目不进入 `answers`。已提交答卷不提供修改操作。

## response_drafts

```javascript
{
  _id: ObjectId("..."),
  survey_id: ObjectId("..."),
  user_id: ObjectId("..."),
  answers: {"a1b2c3d4e5f6a7b8": "是"},
  identity_mode: "named",   // named / anonymous
  revision: 1,
  updated_at: ISODate("2026-10-21T12:03:00Z")
}
```

`(survey_id, user_id)` 唯一，确保同一账号对一份问卷至多保留一条未提交草稿。草稿与正式答卷分开，统计只读取 `responses`。

## 设计与迁移边界

问卷内嵌有序题目，便于一次读取题目及跳转；不断增长的答卷独立存储，避免问卷文档无限膨胀。`question_history`只用于解释本问卷的旧答卷，不是独立题目库。发布、修改题目、关闭及限制一次填写的状态由应用代码维护。实际索引是可重复执行的 `create_index` 调用，无需手动导入 schema 才能运行；新增字段时须同步更新应用校验、旧答卷处理和测试。

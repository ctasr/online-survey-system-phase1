# MongoDB schema（第一阶段结构与第二阶段扩展）

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
  question_history: []      // 保留旧定义，供第一阶段历史答卷继续读取
}
```

`slug` 唯一，`owner_id` 建查询索引。发布时增加 `published_at`，关闭时增加 `closed_at`，均与 `created_at` 分开。多选题另有 `min_select`、`max_select`、`exact_select`；文本题有 `min_length`、`max_length`；数字题有 `min_value`、`max_value`、`integer_only`、`number_statistics`。跳转规则依题型使用选项、选项集合或数字比较条件，并仅指向后续题目。未设置的可选字段可能不存在或为 `null`；详细校验见 [第二阶段设计与变更](第二阶段_设计与变更.md)。

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

`survey_id`、`(viewer_key, submitted_at)`、`(respondent_id, submitted_at)` 建查询索引。仅限制一次提交时，额外写入 `repeat_limited: true` 和不可逆的 `respondent_key`，并对 `(survey_id, respondent_key)` 建局部唯一索引。匿名答卷不存明文账号 ID。`answers` 只保存实际经过的题目：已回答的 `value` 按题型为文字、数字文本或选项数组；经过但未答的选答题保存 `null`，以便区分选答留空和条件跳过。被跳过的题目不进入 `answers`。已提交答卷不提供修改操作。数字原文保留，数字处理上限为输入2050字符、整数与有效小数部分各1000位。历史超限或损坏数字不改写原文，统计返回invalid_count单独说明，不将其算作选答留空。该计数由读取时计算，不新增集合或持久统计副本。

## response_drafts

```javascript
{
  _id: ObjectId("..."),
  survey_id: ObjectId("..."),
  user_id: ObjectId("..."),
  answers: {"q_a1b2c3d4e5f6a7b8": "是"},
  identity_mode: "named",   // named / anonymous
  revision: 1,
  updated_at: ISODate("2026-10-21T12:03:00Z")
}
```

`(survey_id, user_id)` 唯一，确保同一账号对一份问卷至多保留一条未提交草稿。草稿与正式答卷分开，统计只读取 `responses`。

## 设计与迁移边界

问卷内嵌有序题目，便于一次读取题目及跳转；不断增长的答卷独立存储，避免问卷文档无限膨胀。`question_history`只用于解释本问卷的旧答卷，不是独立题目库。发布、关闭及限制一次填写的状态由应用代码维护。实际索引是可重复执行的 `create_index` 调用，无需手动导入 schema 才能运行；新增字段时须同步更新应用校验、旧答卷处理和测试。

## 第二阶段：question_library

```javascript
{
  _id: ObjectId("..."),
  root_id: ObjectId("..."),       // 同一道题的所有版本共享此 ID
  version: 2,                    // 在 root_id 内递增
  parent_id: ObjectId("..."),     // 当前版从哪一版修改而来；首版为 null
  owner_id: ObjectId("..."),      // users._id
  shared: false,                 // 是否向其他登录用户开放此版本
  type: "single_choice",
  title: "你最喜欢哪个季节？",
  required: true,
  options: ["春天", "夏天", "秋天", "冬天"],
  created_at: ISODate("2026-10-23T00:00:00Z")
}
```

多选、文本、数字题使用原有对应校验字段。恢复旧版时会创建新文档，并增加 `restored_from_id` 指向恢复来源。独立版本的定义创建后不原位改写；共享状态可由所有者切换。索引为 `(root_id, version)` 唯一、`(owner_id, created_at)`、`(shared, created_at)`。

## 第二阶段：question_banks

```javascript
{
  _id: ObjectId("..."),
  owner_id: ObjectId("..."),
  name: "常用调查",
  question_ids: [ObjectId("..."), ObjectId("...")], // 准确的题目版本 ID
  created_at: ISODate("2026-10-23T00:00:00Z")
}
```

`(owner_id, name)` 唯一。题库替换版本时以当前 `question_ids` 列表作为条件写入，并在冲突后重读重试，不使用问卷的 `revision` 字段。题库只组织引用，改名或删除不会删除题目或问卷；更新题库条目到新版本也不会自动改变已存在问卷。

## 第二阶段：问卷题目增加的来源字段

从题目库选题时，`surveys.questions[]` 保留第一阶段的 `id`、`version`、题型、校验、跳转等字段，同时增加：

```javascript
{
  library_question_id: ObjectId("..."),
  library_root_id: ObjectId("..."),
  library_version: 2,
  library_source_question_version: 1
}
```

前三项表示所选独立题目版本；最后一项记录该来源对应的问卷内题目版本，供跨问卷统计与答卷版本匹配。草稿直接修改已选题目内容时会解除来源关联，防止把修改后的答案错算入原题。第一阶段旧问卷不含这些字段仍可读写。将已有题目入库不改变题目定义或已提交答卷，草稿会在增加来源标记的同一次写入中递增revision，防止并发编辑覆盖来源。已发布或已关闭问卷入库不改变revision，发布后题目及跳转固定。对surveys的questions.library_question_id字段建立查询索引，用于查找引用指定库题版本的问卷。
## 模拟示例的附加标记

tests/campus_fixtures.py中的模拟数据构造函数只在新生成文档中保存 `demo_seed_suite`、`demo_key`、`demo_generated_at`、`demo_timeline_simulated`。前两项标识示例批次及防重复键，后两项保留实际生成时间并说明展示日期为模拟时间线。这些字段不参与题目权限、校验、跳转或统计，不新增集合，也不修改已有文档。

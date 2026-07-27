# 客户端消息推送逻辑分析（测试环境改造版）

> 整理日期：2026-07-15  
> 项目：sug-manage  
> 模块：manage-service  
> 分支状态：**测试环境，未上线**

---

## 一、改造概览（相比线上版本的变更）

| 改造项 | 线上版本 | 测试版本（当前分支） |
|--------|----------|---------------------|
| **推送渠道** | 仅 App Push (FCM) | App Push + **邮件推送 (AWS SES)** |
| **推送模式** | 仅按用户/Topic | **指定人群(人群包)** / 不限人群 / Topic订阅 |
| **站内信** | 无 | **自动生成站内信记录** (station_letter表) |
| **触发类型** | appoint/sign/drama/subscription | sign/drama/**offers(付费优惠)**/**activity(活动提醒)** (appoint/subscription/msg 标记 @Deprecated) |
| **推送图片** | 无 | **支持图片URL** (fileUrl/fileId) |
| **定时任务** | 单一 push Job | **push (App内推送)** + **emailPush (邮件推送)** 两个独立 Job |
| **用户筛选** | 仅按 push_user 表绑定 | **人群包条件筛选** + 手动指定用户 + 活跃时间过滤 |
| **时间处理** | 服务端本地时间 | **前端传 UTC 时间，服务端转换** |

---

## 二、整体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                        后台管理 (PushMsgController)                    │
│  推送配置 CRUD + 人群包管理 (CrowdPackController) + 站内信查询         │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────────────┐
│                     定时调度 (TimerHandler / XXL-Job)                  │
│  ┌─────────────────┐    ┌─────────────────┐                         │
│  │ push (App推送)   │    │ emailPush (邮件) │                         │
│  └────────┬────────┘    └────────┬────────┘                         │
└───────────┼──────────────────────┼──────────────────────────────────┘
            │                      │
┌───────────▼──────────┐  ┌───────▼───────────────────────────────────┐
│  Firebase (FCM)       │  │  AWS SES (邮件)                          │
│  Token精准 / Topic广播 │  │  SQS → Consumer → SES → email_send_log  │
└───────────────────────┘  └──────────────────────────────────────────┘
            │
┌───────────▼──────────────────────────────────────────────────────────┐
│  站内信 (station_letter) — 每次推送自动生成，APP端可查询/标记已读      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 三、核心数据模型

### 3.1 sys_push (推送主表) — 新增字段

| 字段 | 类型 | 说明 | 新增? |
|------|------|------|-------|
| id | Long | 主键 | |
| push_title | String | 推送标题 | |
| trigger_type_code | String | 触发类型code | |
| trigger_type | String | 触发类型中文名 | |
| trigger_condition | String | 触发条件 | |
| push_msg | String | 推送内容 | |
| jump_interface | String | 跳转界面: 0首页/1剧集页/2活动签到页/3充值页/4收藏页/5FOR U页 | |
| subscription_topic_id | Long | 订阅主题字典ID | |
| play_id | Long | 短剧ID | |
| play_name | String | 短剧名 | |
| series_id | Long | 剧集ID | |
| series_name | String | 剧集名 | |
| trigger_time | Date | 触发时间(计算得出) | |
| drama_online_time | Date | 新剧上线时间 | |
| sign_time | Time | 签到时间(HH:mm:ss) | |
| drama_push_time | Date | 短剧推送时间 | |
| subscription_expire | Integer | 订阅到期小时数 | |
| states | Boolean | 状态: true开启/false关闭 | |
| lang_id | Long | 语言ID | |
| channel | Integer | 推送渠道: 0-App内推送, 1-邮件 | ✅ |
| email_title | String | 邮件标题 | ✅ |
| email_body | String | 邮件正文HTML | ✅ |
| push_mode | Integer | 推送模式: 0-指定人群, 1-不限人群, 2-Topic订阅 | ✅ |
| file_url | String | 推送图片URL | ✅ |
| file_id | Long | 图片文件ID | ✅ |

### 3.2 sys_push_crowd_pack_rel (推送-人群包关联表) ✅新增

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Long | 主键 |
| push_id | Long | 推送配置ID |
| crowd_pack_id | Long | 人群包ID |

### 3.3 crowd_pack (人群包表)

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Long | 主键 |
| name | String | 人群包名称 |
| pay_types | List\<Integer\> | 付费类型: -1=未付费, 0=金币, 1=VIP, 2=通行证 |
| vip_user | Boolean | 是否VIP |
| register_time_compare_type | Integer | 注册时间比较: 1=≥, 2=≤, 3=区间 |
| register_time_min / max | Integer | 注册时间范围(小时) |
| watch_ad_num_compare_type | Integer | 看广告次数比较类型 |
| watch_ad_num_min / max | Integer | 看广告次数范围 |
| watch_duration_compare_type | Integer | 总观看时长比较类型 |
| watch_duration_min / max | Integer | 总观看时长范围(小时) |

### 3.4 station_letter (站内信表) ✅新增

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Long | 主键 |
| user_id | Long | 接收用户ID |
| push_id | Long | 关联推送配置ID |
| message_id | String | 全局唯一消息ID (格式: push_{pushId}_{dateTag}_user_{userId}) |
| trigger_type_code | String | 触发类型 |
| title | String | 消息标题 |
| content | String | 消息内容摘要 |
| image_url | String | 消息图片URL |
| jump_url | String | 跳转链接 |
| jump_type | Integer | 跳转类型: 0-内部页面, 1-H5链接 |
| is_read | Integer | 是否已读: 0-未读, 1-已读 |
| sent_time | LocalDateTime | 发送时间 |
| read_time | LocalDateTime | 已读时间 |

### 3.5 email_send_log (邮件发送流水表) ✅新增

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Long | 主键 |
| push_id | Long | 关联推送配置ID |
| user_id | Long | 接收用户ID |
| recipient_email | String | 收件人邮箱 |
| email_title | String | 邮件标题 |
| send_status | String | 发送状态: SUCCESS / FAIL |
| error_message | String | 失败错误信息 |
| sent_time | LocalDateTime | 发送时间 |

### 3.6 其他已有表

| 表 | 说明 |
|----|------|
| push_user | 推送-用户关联表 (push_id, user_id) |
| user_device | 用户设备表 (user_id, device_id, token, device_sys, time_city) |

---

## 四、推送触发类型（改造后）

| Code | 枚举 | 含义 | 状态 | 时间筛选逻辑 |
|------|------|------|------|-------------|
| `sign` | TRIGGER_TYPE_SIGN | 签到 | ✅启用 | 时分秒比较，支持跨天窗口 |
| `drama` | TRIGGER_TYPE_DRAMA_PUSH | 新剧推送 | ✅启用 | triggerTime 完整时间区间判断 |
| `offers` | TRIGGER_TYPE_PAID_OFFERS | 付费优惠 | ✅新增 | triggerTime 完整时间区间判断 |
| `activity` | TRIGGER_TYPE_ACTIVITY_NOTIFY | 活动提醒 | ✅新增 | triggerTime 完整时间区间判断 |
| `appoint` | TRIGGER_TYPE_APPOINT | 新剧预约 | @Deprecated | 不再参与定时筛选 |
| `subscription` | TRIGGER_TYPE_SUBSCRIPTION_EXPIRE | 订阅到期 | @Deprecated | 不再参与定时筛选 |
| `msg` | TRIGGER_TYPE_SYSTEM_MESSAGE | 系统消息 | @Deprecated | 不再参与定时筛选 |

---

## 五、推送渠道与模式

### 5.1 推送渠道 (PushChannelEnum)

| Code | 说明 | 定时Job |
|------|------|---------|
| 0 | App内推送 (FCM) | `push` |
| 1 | 邮件推送 (AWS SES) | `emailPush` |

### 5.2 推送模式 (pushMode)

| Code | 说明 | 适用渠道 | 用户获取方式 |
|------|------|----------|-------------|
| 0 | 指定人群 | App + Email | 人群包筛选 ∪ 手动指定用户 (push_user表) |
| 1 | 不限人群 | App + Email | 所有活跃用户 (最近N个月内登录) |
| 2 | Topic订阅 | 仅App | FCM Topic广播 (邮件不支持) |

---

## 六、推送配置管理 (后台 CRUD)

### 6.1 PushMsgController API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/push/page` | 分页查询 (支持按人群包ID/剧名/语言筛选) |
| GET | `/push/{id}` | 详情查询 (含人群包名称/用户列表/渠道名称) |
| POST | `/push` | 新增推送配置 |
| PUT | `/push` | 修改推送配置 |
| PUT | `/push/states/{id}` | 启用/禁用 |

### 6.2 新增推送逻辑 (addSysPush) — 改造后

```
1. 校验 triggerTypeCode
2. 并行获取: 短剧名 + 剧集名 + 文件URL (CompletableFuture)
3. 并行获取: 语言ID (从订阅Topic字典映射)
4. DTO → DAO 转换 (PushConvert)
5. 统一处理触发时间: parseUtcTriggerTime() → processTriggerTime()
6. 设置新字段: channel, emailTitle, emailBody, pushMode, fileUrl
7. 保存主记录
8. ★ 保存人群包关联: saveCrowdPackRels(pushId, pushMode, crowdPackIds)
   - 仅 pushMode=0 时保存
9. 保存推送用户关联: savePushUsers(pushId, userIds, multiUserIds)
```

### 6.3 修改推送逻辑 (updateSysPush) — 改造后

```
1. 校验 ID + 记录存在性
2. ★ pushMode 变更时清空不相关参数:
   - 非 Topic 模式 → 清空 subscriptionTopicId, langId
3. triggerTypeCode 变更时清空不相关字段 (clearIrrelevantFields)
4. 并行获取基本信息 + 语言
5. 转换 + 处理触发时间
6. 更新主记录
7. 先删后增人群包关联 + 推送用户关联
```

### 6.4 分页查询逻辑 (queryPage) — 改造后

```
1. 按剧名模糊搜索 → 获取 playIds
2. ★ 按人群包ID搜索 → 获取关联的 pushIds
3. 按语言ID搜索 → 获取 topicIds
4. 执行分页查询
5. 组装返回字段:
   - ★ 人群包名称 (从 sys_push_crowd_pack_rel → crowd_pack 取 name，逗号分隔)
   - 短剧信息、语言信息
   - ★ 推送渠道名称 (PushChannelEnum)
   - ★ 人群包展示: pushMode=1 → "不限"，pushMode=0 → 人群包名称
```

---

## 七、人群包管理 (CrowdPackController)

### 7.1 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/crowd-pack` | 分页查询 |
| GET | `/crowd-pack/{id}` | 详情 |
| GET | `/crowd-pack/{id}/usage` | 使用情况 |
| POST | `/crowd-pack` | 新增 |
| PUT | `/crowd-pack` | 编辑 |
| DELETE | `/crowd-pack/{id}` | 删除 |
| GET | `/crowd-pack/register-time-options` | 注册时间筛选选项 |

### 7.2 人群包筛选维度

| 维度 | 条件类型 | 说明 |
|------|----------|------|
| 付费类型 | 多选 | -1=未付费, 0=金币, 1=VIP, 2=通行证 |
| VIP用户 | 布尔 | 是否VIP |
| 注册时间 | 比较+区间 | 支持 ≥ / ≤ / 区间，单位小时 |
| 看广告次数 | 比较+区间 | 支持 ≥ / ≤ / 区间 |
| 总观看时长 | 比较+区间 | 支持 ≥ / ≤ / 区间，单位小时 |

### 7.3 核心方法

| 方法 | 说明 |
|------|------|
| `getMatchedCrowdPackIdsByUserId(userId)` | 匹配用户满足的人群包ID列表 |
| `filterUsersByCrowdPack(page, crowdPackIds, extraUserIds, lastActiveThreshold)` | 按人群包分页筛选用户 |
| `filterUsersByPushCrowdConditions(page, sysPush, extraUserIds, lastActiveThreshold)` | 按推送配置中的人群包条件筛选用户 |
| `validateCrowdConflict(crowdIds, conflictCrowdIds)` | 验证人群包冲突 |

---

## 八、站内信管理 (AppStationLetterController)

### 8.1 API (APP端)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/app/station/letter/page` | 分页获取站内信列表 |
| GET | `/app/station/letter/unread/count` | 获取未读消息数 |
| POST | `/app/station/letter/{messageId}/read` | 标记单条已读 |
| POST | `/app/station/letter/read/all` | 一键全部已读 |

### 8.2 站内信生成逻辑

在定时推送执行时，**无论哪种推送模式**，都会为每个目标用户生成站内信记录：

```
messageId = "push_{pushId}_{dateTag}_user_{userId}"
  - dateTag = 当天日期去横线 (如 20260715)
  - 同一规则同一天对同一用户不会重复插入 (查询已有 messageId 去重)

站内信字段:
  - title = sysPush.pushTitle
  - content = sysPush.pushMsg
  - imageUrl = sysPush.fileUrl
  - jumpUrl = sysPush.jumpInterface
  - jumpType = activity类型→1(H5), 其他→0(内部页面)
  - isRead = 0 (未读)
  - sentTime = 批次统一时间
```

---

## 九、定时推送调度（改造后核心链路）

### 9.1 两个独立 Job

| Job | 入口 | 处理方法 | 筛选渠道 |
|-----|------|----------|----------|
| `push` | TimerHandler.push() | TimerServiceImpl.push() | channel=0 (App内推送) |
| `emailPush` | TimerHandler.emailPush() | TimerServiceImpl.pushEmail() | channel=1 (邮件推送) |

### 9.2 App内推送主流程 (push → handleAppPush)

```
1. 查询所有启用的推送规则, 过滤 channel=0
2. filterTriggerPushList() 筛选 [now, now+5min] 内需触发的规则
3. 遍历每条规则 → handleAppPush(sysPush, twoMonthsAgo):

   ┌─ pushMode=2 (Topic订阅) ─────────────────────────────────┐
   │  1. pushByTopic() → FCM Topic广播                        │
   │  2. 分批迭代用户 → 仅生成站内信 (不执行Token精准推送)       │
   └───────────────────────────────────────────────────────────┘

   ┌─ pushMode=0/1 (指定人群/不限人群) ────────────────────────┐
   │  分批迭代用户 (每批500):                                   │
   │  1. getTargetUserIds() → 获取目标用户分页                  │
   │  2. 生成站内信记录 (去重)                                  │
   │  3. getUserDeviceTokens() → 获取用户设备Token              │
   │  4. 构建 UserPushMsgDTO (token + data.messageId)          │
   │  5. fireBaseService.pushToUsers() → 精准推送              │
   └───────────────────────────────────────────────────────────┘
```

### 9.3 邮件推送主流程 (pushEmail → handleEmailPush)

```
1. 查询所有启用的推送规则, 过滤 channel=1
2. filterTriggerPushList() 筛选时间窗口
3. 遍历每条规则 → handleEmailPush(sysPush, twoMonthsAgo):

   ┌─ pushMode=2 (Topic订阅) ─────────────────────────────────┐
   │  ⚠️ 邮件不支持Topic模式, 计入失败                          │
   └───────────────────────────────────────────────────────────┘

   ┌─ pushMode=0/1 (指定人群/不限人群) ────────────────────────┐
   │  分批迭代用户 (每批1000):                                  │
   │  1. getTargetUserIds() → 获取目标用户分页                  │
   │  2. getUserEmails() → 获取用户邮箱 (user_bind表)           │
   │  3. 无邮箱用户 → 记录 FAIL 日志到 email_send_log           │
   │  4. emailService.sendEmails() → 发送SQS消息               │
   └───────────────────────────────────────────────────────────┘
```

### 9.4 目标用户获取 (getTargetUserIds)

```
pushMode=0 (指定人群):
  1. 获取 push_user 表中手动绑定的用户ID列表 (extraUserIds)
  2. crowdPackService.filterUsersByPushCrowdConditions(page, sysPush, extraUserIds, twoMonthsAgo)
     → 人群包条件筛选 ∪ 手动指定用户, 过滤活跃时间

pushMode=1/2 (不限人群/Topic订阅):
  → userRepository.findActiveUsersByPage(page, twoMonthsAgo)
     → 所有最近N个月内活跃的用户
```

### 9.5 时间筛选逻辑 (filterTriggerPushList)

```
邮件渠道 (channel=1):
  → 直接用 triggerTime 判断完整时间区间 [now, now+5min]

App渠道 (channel=0):
  sign    → 时分秒比较 (isSignTimeInRange, 支持跨天窗口)
  drama   → triggerTime 完整时间区间判断
  offers  → triggerTime 完整时间区间判断
  activity→ triggerTime 完整时间区间判断
  其他    → 不参与定时筛选 (返回 false)
```

### 9.6 FCM 推送数据载荷 (改造后)

```java
// 所有推送都携带:
data.put("messageId", messageId);  // ★ 新增: 站内信消息ID, APP端点击可跳转

// appoint/drama 类型额外携带:
data.put("playId", playId);
data.put("seriesId", seriesId);
```

---

## 十、邮件推送执行链路 (AWS SES + SQS)

```
TimerServiceImpl.handleEmailPush()
    │
    ▼
EmailServiceImpl.sendEmails(pushConfig, targetUserEmails)
    │
    ├── 分批 (500/批) 构建 EmailSqsDTO
    │   └── {pushId, userId, recipientEmail, emailTitle, emailBody}
    │
    ▼
EmailSqsProducer.send(emailSqsDTO)          // 发送到 SQS 队列
    │  (JmsTemplate → SQS queue: push_email)
    │
    ▼
EmailSqsConsumer.receiveMessage(messages)   // 消费 SQS 消息
    │
    ▼
sendBulkEmailViaSes(emailDtos)
    │
    ├── 逐封发送:
    │   ├── AmazonSimpleEmailService.sendEmail(request)
    │   │   └── from: ${aws.ses.from_email_address}
    │   │       to: recipientEmail
    │   │       subject: emailTitle (UTF-8)
    │   │       body: emailBody (HTML, UTF-8)
    │   │
    │   └── 记录流水 → EmailSendLogRepository.save()
    │       ├── SUCCESS: sendStatus="SUCCESS"
    │       └── FAIL:    sendStatus="FAIL", errorMessage=异常信息
    │
    └── 日志: SES bulk send completed. Success: X, Failure: Y
```

> ⚠️ **注意**: 当前 `EmailServiceImpl` 中 `emailSqsProducer.send()` 被注释掉了，邮件实际不会发送！

---

## 十一、签到时间跨天窗口处理（改造优化）

线上版本签到时间只做简单的"今天/明天"判断，改造版优化了跨天场景：

```java
// 将时分秒转为当天秒数进行数值比较
int nowSeconds = hour*3600 + minute*60 + second;

if (nowSeconds <= advanceSeconds) {
    // 正常窗口: 签到时间在 [now, advance) 之间
    return signSeconds >= nowSeconds && signSeconds < advanceSeconds;
} else {
    // 跨天窗口 (如 23:58 ~ 00:03):
    // 签到时间在 [now, 24:00) 或 [00:00, advance) 内均满足
    return signSeconds >= nowSeconds || signSeconds < advanceSeconds;
}
```

---

## 十二、完整推送时序图（改造后）

### App内推送 (push Job)

```
XXL-Job "push"
    │
    ▼
TimerServiceImpl.push()
    │
    ├── 1. pushRepository.getUsableList() → filter channel=0
    ├── 2. filterTriggerPushList() → 筛选时间窗口 [now, now+5min]
    │
    └── for each rule → handleAppPush():
        │
        ├── [pushMode=2] → pushByTopic() → FCM Topic广播
        │
        ├── 分批迭代用户 (500/批):
        │   │
        │   ├── getTargetUserIds():
        │   │       ├── [mode=0] crowdPackService.filterUsersByPushCrowdConditions() + push_user
        │   │       └── [mode=1/2] userRepository.findActiveUsersByPage()
        │   │
        │   ├── ★ 生成站内信 (station_letter):
        │   │       ├── messageId = push_{pushId}_{dateTag}_user_{userId}
        │   │       ├── 查询已有 messageId 去重
        │   │       └── stationLetterRepository.saveBatch()
        │   │
        │   └── [pushMode≠2] Token精准推送:
        │           ├── getUserDeviceTokens() → 按语言筛选 + 查设备Token
        │           ├── 构建 UserPushMsgDTO (token + data.messageId)
        │           └── fireBaseService.pushToUsers()
        │
        └── return [successCount, failureCount]
```

### 邮件推送 (emailPush Job)

```
XXL-Job "emailPush"
    │
    ▼
TimerServiceImpl.pushEmail()
    │
    ├── 1. pushRepository.getUsableList() → filter channel=1
    ├── 2. filterTriggerPushList() → 筛选时间窗口
    │
    └── for each rule → handleEmailPush():
        │
        ├── [pushMode=2] → ⚠️ 邮件不支持Topic, 计入失败
        │
        └── [pushMode=0/1] 分批迭代用户 (1000/批):
            │
            ├── getTargetUserIds() → 同上
            ├── getUserEmails() → 查询用户邮箱 (user_bind表)
            ├── 无邮箱用户 → 记录 FAIL 到 email_send_log
            └── emailService.sendEmails()
                    │
                    ├── 构建 EmailSqsDTO (500/批)
                    ├── EmailSqsProducer.send() → SQS队列
                    │       │
                    │       ▼
                    └── EmailSqsConsumer.receiveMessage()
                            │
                            └── SES.sendEmail() → 记录 email_send_log
```

---

## 十三、关键配置项

| 配置 | 说明 |
|------|------|
| `suguang.push.someMin4push` | 定时推送提前量(分钟), 默认5 |
| `suguang.push.activeMonthLimit` | 活跃用户月数阈值, 默认2 |
| `sqs.queue.push_email.name` | 邮件SQS队列名 |
| `aws.ses.from_email_address` | SES发件人地址 |
| `module-switch.firebase` | Firebase模块开关 |

---

## 十四、核心代码文件索引

| 文件 | 路径 | 说明 |
|------|------|------|
| PushMsgController | sysconfig/application/PushMsgController.java | 推送配置后台API |
| CrowdPackController | sysconfig/application/CrowdPackController.java | 人群包管理API |
| AppStationLetterController | letter/application/app/AppStationLetterController.java | 站内信APP端API |
| PushMsgServiceImpl | sysconfig/domain/service/impl/PushMsgServiceImpl.java | 推送配置业务逻辑 |
| CrowdPackServiceImpl | sysconfig/domain/service/impl/CrowdPackServiceImpl.java | 人群包业务逻辑 |
| EmailServiceImpl | sysconfig/domain/service/impl/EmailServiceImpl.java | 邮件发送服务 |
| TimerServiceImpl | timer/impl/TimerServiceImpl.java | 定时推送调度核心 |
| TimerHandler | timer/handler/TimerHandler.java | XXL-Job入口 |
| FireBaseServiceImpl | sysconfig/domain/service/impl/FireBaseServiceImpl.java | FCM推送执行 |
| EmailSqsProducer | aws/sqs/producer/EmailSqsProducer.java | 邮件SQS生产者 |
| EmailSqsConsumer | aws/sqs/consumer/EmailSqsConsumer.java | 邮件SQS消费者(SES发送) |
| PushChannelEnum | sysconfig/domain/infrastructure/constants/enums/PushChannelEnum.java | 推送渠道枚举 |
| PushMsgEnum | sysconfig/domain/infrastructure/constants/enums/PushMsgEnum.java | 触发类型枚举 |
| SysPushDAO | sysconfig/domain/infrastructure/dao/SysPushDAO.java | 推送主表DAO |
| SysPushCrowdPackRelDAO | sysconfig/domain/infrastructure/dao/SysPushCrowdPackRelDAO.java | 推送-人群包关联DAO |
| CrowdPackDAO | sysconfig/domain/infrastructure/dao/CrowdPackDAO.java | 人群包DAO |
| StationLetterDAO | letter/domain/infrastructure/dao/StationLetterDAO.java | 站内信DAO |
| EmailSendLogDAO | sysconfig/domain/infrastructure/dao/EmailSendLogDAO.java | 邮件流水DAO |

---

## 十五、现有问题与注意事项

1. **⚠️ EmailSqsProducer.send() 被注释**: `EmailServiceImpl` 中 `emailSqsProducer` 的 `@Autowired` 和 `send()` 调用均被注释，邮件实际**不会发送**，仅打印日志
2. **权限注解大部分注释**: `PushMsgController` 和 `CrowdPackController` 的 `@PreAuthorize` 均已注释
3. **邮件不支持Topic模式**: `handleEmailPush` 中 pushMode=2 直接计入失败，但后台配置未做前端限制
4. **人群包 + 手动用户合并逻辑**: pushMode=0 时，人群包筛选结果与 push_user 表手动指定的用户取**并集** (extraUserIds)
5. **站内信去重机制**: 通过 messageId (含 dateTag) 保证同一规则同一天对同一用户不重复插入，但**跨天可重复**
6. **FCM data.messageId**: 改造后 FCM 推送 payload 中新增 messageId 字段，APP端点击通知可跳转到对应站内信
7. **活动提醒跳转类型**: triggerTypeCode=activity 时站内信 jumpType=1 (H5链接)，其余为0 (内部页面)
8. **CompletableFuture 并行**: 新增/修改推送时使用并行获取短剧名/剧集名/文件URL，但未指定自定义线程池
9. **Deprecated 触发类型**: appoint/subscription/msg 虽然标记 @Deprecated 但枚举仍保留，数据库可能存在旧数据
10. **签到跨天优化**: 改造后签到时间比较改为纯秒数比较，解决了跨午夜窗口漏触发问题
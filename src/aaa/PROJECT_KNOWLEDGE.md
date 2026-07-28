# 项目知识库 — suguang (SarosTV) 短剧平台

> 最后更新: 2026-07-16
> 用途: AI 压缩上下文后的回忆参考文档

---

## 一、项目概述

这是一个**短剧/视频流媒体平台**的后端系统，产品名 **SarosTV**，面向全球用户（多语言、多国家）。
核心业务：短剧内容管理、广告归因投放、支付变现、消息推送、A/B 实验、TikTok 小程序。

---

## 二、技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Java 8 |
| 框架 | Spring Boot 2.6.15 + Spring Cloud 2021.0.9 |
| 微服务 | Spring Cloud Alibaba (Nacos 服务发现 + 配置中心) |
| 安全 | Spring Security + JWT (Hutool HS256) |
| ORM | MyBatis Plus 3.5.5 + MySQL + Druid |
| 缓存 | Redis + Redisson (分布式锁) |
| 数据仓库 | ClickHouse |
| 消息队列 | AWS SQS (FIFO) |
| 对象存储 | AWS S3 |
| CDN | AWS CloudFront |
| 邮件 | AWS SES |
| 推送 | Firebase Cloud Messaging (FCM) |
| 支付 | Stripe / PayPal / Alipay Global (Antom) |
| 广告 | Google Ads API / Facebook Business SDK / TikTok Business API |
| 归因 | AppsFlyer (AF) Push API |
| 定时任务 | XXL-Job |
| DTO 映射 | MapStruct + Lombok |
| API 文档 | Knife4j / Swagger |
| IP 定位 | GeoIP2 |

---

## 三、模块结构

```
app/                            (Maven 多模块根项目)
├── pom.xml                     (父 POM, packaging=pom)
├── dependencies/pom.xml        (BOM — 统一依赖版本管理)
├── framework/common/           (共享库: BaseDAO, Redis, i18n, 异常处理, Swagger)
├── gateway/                    (Spring Cloud Gateway, 端口 7999)
│   └── filter/LoggingFilter    (GeoIP 国家过滤)
├── datacenter/                 (Google Ads 数据采集服务, 端口 8200)
│   └── google/service/         (Google Ads API 集成)
└── manage/manage-service/      (核心业务服务 — 主应用)
    ├── aws/sqs/                (SQS 生产者/消费者)
    ├── config/                 (Security, JWT, CORS, Stripe, PayPal, AWS, 线程池)
    ├── content/                (短剧/视频内容管理)
    ├── creator/                (创作者/企业入驻)
    ├── data/                   (归因, 事件上报, 广告数据)
    ├── experiment/             (A/B 测试框架)
    ├── facebook/               (Facebook 广告集成)
    ├── google/                 (Google AdMob 集成)
    ├── letter/                 (站内信)
    ├── novel/                  (小说/内容分发, QM 集成)
    ├── pay/                    (支付: Stripe, PayPal, Antom, 订单, 钱包)
    ├── sysconfig/              (系统配置: Banner, 弹窗, 活动, 人群包, 推荐)
    ├── tiktok/                 (TikTok 广告集成)
    ├── tiktokminis/            (TikTok 小程序集成)
    ├── timer/                  (定时任务: XXL-Job handlers)
    └── user/                   (用户管理, 认证, 角色, 权限)
```

---

## 四、分层架构 (DDD-lite)

每个业务模块遵循统一的分层模式:

```
module/
├── application/          → Controller (app/web/feign 三种入口)
│   ├── app/              → App 客户端 API
│   ├── web/              → 后台管理 API
│   └── feign/            → 微服务间调用
└── domain/
    ├── entity/           → 领域实体
    ├── service/          → 业务接口
    │   └── impl/         → 业务实现
    ├── repository/       → 数据访问接口
    │   └── impl/         → 数据访问实现
    └── infrastructure/
        ├── dao/          → MyBatis Plus DAO
        ├── mapper/       → MyBatis Mapper
        ├── dto/          → 数据传输对象
        ├── vo/           → 视图对象
        ├── mapstruct/    → MapStruct 转换器
        ├── enums/        → 枚举
        └── constants/    → 常量
```

---

## 五、路由结构

**Gateway (端口 7999)** → Nacos 路由配置 → manage-service (context: `/sarosTv`)

| URL 前缀 | 受众 | 用途 |
|----------|------|------|
| `/app/user/**` | App | 用户登录/注册 |
| `/app/play/**` | App | 视频播放/历史 |
| `/app/series/**` | App | 剧集浏览 |
| `/app/order/**` | App | 订单/订阅 |
| `/app/shop/**` | App | 商城 |
| `/app/banner/**` | App | Banner |
| `/app/popup/**` | App | 弹窗 |
| `/app/st/**` | App | Stripe 支付 |
| `/app/paypal/**` | App | PayPal 支付 |
| `/app/antom/**` | App | Antom 支付 |
| `/app/station/letter/**` | App | 站内信 |
| `/app/tiktok/minis/**` | TikTok 小程序 | 小程序 API |
| `/user/**` | 后台 | 用户管理 |
| `/play/**` | 后台 | 内容管理 |
| `/series/**` | 后台 | 剧集管理 |
| `/order/**` | 后台 | 订单管理 |
| `/push/**` | 后台 | 推送管理 |
| `/crowd-pack/**` | 后台 | 人群包管理 |
| `/af/**` | 外部 | AppsFlyer 回调 |
| `/fb/callback/**` | 外部 | Facebook OAuth |
| `/data/**` | 内部 | 数据/归因 |
| `/timer/**` | 内部 | 定时任务触发 |
| `/web/**` | Web | Web 门户 |
| `/out/**` | Feign | 微服务间调用 |

---

## 六、认证与安全

### 后台管理员认证
- JWT 无状态认证 (HS256, Hutool)
- Token 存 Redis + 数据库 (`authentication_token` 表)
- `JwtAuthenticationFilter` 验证 Token → 检查过期 → 加载权限 → 填充 `UserContextHolder`
- BCrypt 密码编码
- `@PreAuthorize` 角色权限控制 (部分已注释)

### App 用户认证
- 游客自动注册 (首次登录)
- 三方登录: Facebook / Google / Apple
- TikTok 小程序: 静默登录 (code → access_token)

### 请求上下文
- `UserContextHolder` (TransmittableThreadLocal) 携带: userId, deviceId, deviceSys, langId, deepLink, cloudfront-viewer-country
- 请求头携带: `token`, `lang`, `appVersion`, `deviceSys`, `deviceId`, `deepLink`, `deepIp`, `deepUserAgent`, `cloudfront-viewer-country`, `goodsVersion`

---

## 七、广告归因系统 (核心业务)

### 7.1 概念

- **归因**: 确定每个付费用户来自哪条广告
- **AppsFlyer (AF)**: 第三方归因平台, 充当"裁判"
- **自归因平台 (SAN)**: TikTok / Facebook / Google — "既当运动员又当裁判"
- **染色**: 给用户标记广告来源, 30 天窗口期内所有付费行为归功于该广告
- **W2A (Web-to-App)**: 落地页跳转方式, 能收集 IP/UA
- **直投 (Direct)**: 直接跳转商店, 依赖设备 ID 匹配

### 7.2 归因数据流

```
广告平台点击 → 落地页(收集IP/UA+广告参数) → 用户下载App →
AF SDK 匹配 → AF Push API 推送到后端 → 染色决策 → 事件上报给广告平台
```

- **归因靠 AF**: AF Push API → `AFDataController` → 入库
- **上报直接找平台**: 后端直接调 Facebook/TikTok/Google API (不经过 AF)

### 7.3 染色状态

| 状态 | 说明 | 上报事件? |
|------|------|----------|
| FIRST (1) | 首次染色, 新用户匹配到广告来源 | ✅ |
| REPEAT (2) | 重染色, 超30天+满足条件+新广告 | ✅ |
| ORGANIC (3) | 自然量, 无法确定来源 | ❌ (10分钟内可升级为FIRST) |

### 7.4 事件类型

```java
ACTIVATE(1, "激活", "CompleteRegistration", "CompleteRegistration")
REGISTER(2, "注册", "Lead", null)
WATCHED_ALL_FREE_VIDEO(3, "看完免费剧集", "AddToWishlist", "AddToWishlist")
INITIATED_CHECKOUT(4, "发起充值", "InitiateCheckout", "InitiateCheckout")
PURCHASER(5, "完成充值", "Purchase", "CompletePayment")
SUBSCRIBE(6, "订阅", "Subscribe", null)
```

### 7.5 广告投放层级

```
广告账户 → 广告组(Campaign) → 广告计划(Ad Group) → 广告创意(Ad Creative)
```

---

## 八、支付系统

### 支付方式

| 平台 | 说明 |
|------|------|
| Stripe | 卡支付 / Apple Pay / Google Pay, 支持订阅 |
| PayPal | PayPal 钱包支付 |
| Antom (Alipay Global) | 支付宝国际版 |
| Apple IAP | iOS 内购 |
| Google Play | Android 内购 |

### 商品类型

- **金币 (goods_type=0)**: 虚拟货币, 用于解锁剧集
- **VIP (goods_type=1)**: 订阅服务, 解锁全部内容
- **通行证 (goods_type=2)**: 另一种订阅权益

### 关键表

- `goods` → 付费项定义
- `goods_country` → 国家差异化定价
- `goods_third_discount` → 三方支付折扣
- `sys_order` → 订单主表
- `sys_order_extra` → 订单额外信息 (支付方式/弹窗归因)
- `sys_order_amount` → 订单金额 (本地/USD/CNY)
- `user_balance` → 用户余额 (充值金币 + 赠币)
- `user_change` → 金币变更记录
- `pay_ability_config` → 支付能力配置 (人群包定向)
- `recharge_template_config` → 充值模板配置 (人群包定向)
- `pay_exchange_rate` → 汇率表

---

## 九、消息推送系统

### 9.1 推送渠道

| 渠道 | 技术 | 定时 Job |
|------|------|---------|
| App 内推送 | Firebase FCM | `push` |
| 邮件推送 | AWS SES + SQS | `emailPush` |

### 9.2 推送模式

| 模式 | 说明 | 用户获取 |
|------|------|---------|
| 0 - 指定人群 | 人群包筛选 + 手动指定 | crowd_pack ∪ push_user |
| 1 - 不限人群 | 所有活跃用户 | 最近 N 月内登录 |
| 2 - Topic 订阅 | FCM Topic 广播 | 语言 → Topic 映射 |

### 9.3 触发类型

| Code | 说明 | 状态 |
|------|------|------|
| sign | 签到 | ✅ 启用 |
| drama | 新剧推送 | ✅ 启用 |
| offers | 付费优惠 | ✅ 新增 |
| activity | 活动提醒 | ✅ 新增 |
| appoint | 新剧预约 | @Deprecated |
| subscription | 订阅到期 | @Deprecated |
| msg | 系统消息 | @Deprecated |

### 9.4 推送流程

```
XXL-Job 触发 → 查询启用规则 → 时间窗口筛选 →
  ├─ pushMode=2 → FCM Topic 广播
  └─ pushMode=0/1 → 分批(500)获取用户 → 生成站内信 → 获取设备Token → FCM 精准推送
```

### 9.5 站内信

- 每次推送自动生成 `station_letter` 记录
- messageId = `push_{pushId}_{dateTag}_user_{userId}` (去重)
- APP 端可查询/标记已读

### 9.6 FCM Token 管理

- 客户端 FCM SDK 获取 Token → `POST /app/token` 上报 → 存 `user_device` 表
- 语言隔离: langId → 字典表 `SUBSCRIPTION_TOPIC` → FCM Topic 名称

### 9.7 人群包 (crowd_pack)

**核心维度表**, 被推送/弹窗/活动/支付能力/充值模板/广告策略等模块引用:

筛选维度: 付费类型, VIP状态, 注册时间, 看广告次数, 观看时长, 连续登录天数, 国家, 语言, 渠道, 设备系统, 当日登录

---

## 十、弹窗系统

- `popup` → 弹窗主表 (折扣/剧集推荐/登录/激励视频/自定义)
- `popup_rule` → 弹窗规则 (触发类型/页面/时机/次数/优先级/AB实验)
- `popup_rule_crowd` → 规则-人群包关联
- `popup_rule_lang` → 规则-语言
- `popup_rule_country` → 规则-国家
- `popup_target_rule` → 定向规则 (ALL/USER/VERSION)
- `popup_user_history` → 用户弹窗历史

---

## 十一、A/B 实验系统

- 基于 hash 的流量分配
- Nacos 驱动的实验配置
- 请求级别的实验上下文 (`ExperimentContextFilter`)
- 影响: 推送、弹窗、支付能力、充值模板、广告策略

---

## 十二、TikTok 小程序

- `tt_minis` → 小程序配置 (app_id, client_key, client_secret)
- `tt_minis_user` → 小程序用户 (open_id, access_token)
- 静默登录: code → access_token → 查询/创建用户
- 功能: 剧场浏览, 播放历史, FOR U 推荐, 分享链接

---

## 十三、定时任务 (XXL-Job)

| Job | 说明 |
|-----|------|
| push | App 内推送 |
| emailPush | 邮件推送 |
| vipExpire | VIP 过期处理 |
| dataCounting | 数据统计 |
| fbCostPull | Facebook 广告花费拉取 |
| ttCostPull | TikTok 广告花费拉取 |
| ggCostPull | Google 广告花费拉取 |
| eventPush | 事件推送 |
| userCountry | 用户国家计算 |
| scheduledRelease | 定时发布 |
| imageCompress | 图片压缩 |

---

## 十四、部署架构

```
[客户端] → [CloudFront CDN] → [Gateway:7999] → [Nacos 发现]
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
              [manage-service]  [datacenter]    [其他服务]
                    │               │
                 [MySQL]        [ClickHouse]
                    │
                 [Redis]
```

- Gateway 端口: 7999
- datacenter 端口: 8200
- manage-service context: `/sarosTv`
- Nacos: 服务发现 + 配置中心 (namespace 隔离环境)
- 环境: local / test (devTest) / prod

---

## 十五、数据库概览 (75+ 张表)

| 域 | 表数 | 核心表 |
|----|------|--------|
| 用户与权限 | 15 | sys_user, user_bind, user_device, sys_role, sys_menu, permission |
| 内容(短剧) | 20 | parent_play, play, play_series, sys_tag, sys_type, play_history |
| 推送与站内信 | 7 | sys_push, push_user, crowd_pack, station_letter, email_send_log |
| 弹窗 | 8 | popup, popup_rule, popup_rule_crowd, popup_user_history |
| 支付与订单 | 22 | sys_order, goods, user_balance, pay_ability_config, pay_exchange_rate |
| 系统配置 | 12 | sys_dict, sys_file, theater, banner, recommend, sys_page |
| 活动 | 4 | activity_config, activity_push_rule, activity_push_history |
| 小说 | 4 | novel, novel_chapter |
| 数据分析 | 4 | advertise_data_coloration_new, ad_country_daily_report |
| TikTok小程序 | 11 | tt_minis, tt_minis_user, tt_minis_theater_config |

### 关键关联

- `sys_user` 是用户域核心, 被 30+ 表直接引用
- `play` 是内容域核心, 被 25+ 表引用
- `crowd_pack` 是维度核心, 被推送/弹窗/活动/支付/广告 6 个域引用
- `sys_lang` 实现多语言隔离, 被几乎所有业务主表引用

### BaseDAO 继承字段 (所有表通用)

`create_time`, `update_time`, `create_user`, `update_user`, `version` (乐观锁), `deleted` (逻辑删除)

---

## 十六、开发规范与约定

- REST API 统一返回 `Result<T>` 包装
- DTO ↔ DAO 使用 MapStruct 转换
- 分布式锁使用 Redisson
- 多语言通过 `lang` 请求头 → `LoginUser.langId`
- 国家通过 CloudFront `cloudfront-viewer-country` 请求头
- 异步处理使用 AWS SQS FIFO 队列
- 配置外置到 Nacos (namespace 隔离)

---

## 十七、已知问题 / 注意事项

1. `PushMsgController` 的 `@PreAuthorize` 权限注解全部已注释
2. `EmailSqsProducer.send()` 被注释, 邮件实际不会发送
3. 签到推送跨天窗口已在测试版修复 (纯秒数比较)
4. appoint/subscription 触发类型已 @Deprecated, 不参与定时推送
5. `addSysPush` 和 `updateSysPush` 的 triggerTime 校验逻辑不一致
6. 邮件推送不支持 Topic 模式 (pushMode=2)
7. 站内信 messageId 含 dateTag, 同规则同天同用户不重复, 但跨天可重复

---

*文档结束 — 此文件用于 AI 上下文压缩后的项目回忆参考*

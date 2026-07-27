# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Project Overview

**suguang (SarosTV)** — a short-drama streaming platform backend serving global users across 14 languages. Built with Java 8 / Spring Boot 2.6.15 / Spring Cloud Alibaba (Nacos).

## Build Commands

```bash
# Full build (skip tests — most are commented out)
mvn clean install -DskipTests

# Build single module with dependencies
mvn clean install -pl manage/manage-service -am
mvn clean install -pl gateway -am
mvn clean install -pl datacenter -am

# Run manage-service (main backend, context path: /sarosTv)
java -jar manage/manage-service/target/manage-0.0.1-SNAPSHOT.jar

# Run gateway (port 7999)
java -jar gateway/target/gateway-0.0.1-SNAPSHOT.jar

# Docker build for manage-service
cd manage/manage-service
mvn clean package -DskipTests
docker build -f src/main/resources/Dockerfile -t manage-service .
```

Default Spring profile: `devTest`. Switch with `--spring.profiles.active=local` or `prod`.

## Architecture

### Module Structure

| Module | Purpose |
|---|---|
| `dependencies/` | BOM — centralized dependency versions |
| `framework/common/` | Shared lib: BaseDAO, Redis utils, i18n, exception handling, Swagger config |
| `gateway/` | Spring Cloud Gateway (port 7999), GeoIP country filtering |
| `datacenter/` | Google Ads data collection (port 8200), ClickHouse |
| `manage/manage-service/` | Core business service — all business logic lives here |

### DDD-lite Layered Architecture

Each business module under `manage/manage-service/src/main/java/com/sug/manage/` follows this pattern:

```
module/
├── application/          → Controllers
│   ├── app/              → Mobile APP API  (prefix: /app/...)
│   ├── web/              → Admin backend API
│   └── feign/            → Inter-service calls (prefix: /out/...)
└── domain/
    ├── entity/           → Domain entities
    ├── service/ + impl/  → Business logic
    ├── repository/ + impl/ → Data access abstraction
    └── infrastructure/
        ├── dao/          → MyBatis Plus DAO (table mapping)
        ├── mapper/       → MyBatis Mapper interfaces
        ├── mapperxml/    → MyBatis XML (in resources/mapperxml/)
        ├── dto/          → Data Transfer Objects
        ├── vo/           → View Objects
        ├── mapstruct/    → MapStruct converters
        ├── enums/        → Enumerations
        └── constants/    → Constants
```

### Key Business Modules

| Directory | Domain |
|---|---|
| `user/` | User auth, login (guest/third-party), roles, permissions |
| `content/` | Short dramas, episodes, tags, types, playback history |
| `pay/` | Orders, goods, Stripe/PayPal/Antom payments, wallets |
| `data/` | Ad attribution (AppsFlyer), event reporting to FB/TikTok/Google |
| `experiment/` | A/B testing framework (hash-based traffic, Nacos-driven) |
| `sysconfig/` | Banners, popups, push notifications, crowd packs, recommendations |
| `tiktokminis/` | TikTok Mini Program integration |
| `letter/` | In-app station letters (generated on push) |
| `timer/` | XXL-Job scheduled tasks |
| `novel/` | Novel content management |

### Critical Data Flow — Ad Attribution

This is the most complex business flow. It is NOT a simple CRUD:

```
Ad click → Landing page (collect IP/UA + ad params) → User downloads App →
AF SDK match → AF Push API → Backend (AFDataController) → Coloration decision
(FIRST/REPEAT/ORGANIC) → Event reporting to FB/TikTok/Google APIs directly
```

- Attribution data comes IN via AppsFlyer Push API (`/af` endpoint)
- Events go OUT directly to ad platform APIs (NOT through AF)
- Coloration state determines whether events are reported

### Multi-Audience Controllers

Each module has separate controllers for different audiences sharing the same services:
- `application/app/` — Mobile app endpoints (`/app/...`)
- `application/web/` — Admin backend endpoints
- `application/feign/` — Microservice-to-microservice (`/out/...`)

## Configuration

**Almost all runtime config lives in Nacos**, not in local YAML files. The `bootstrap.yaml` files only contain Nacos connection details. Key Nacos namespaces:
- Test: `472afd5d-...` at `13.219.217.87:8848`
- Extension configs loaded from Nacos: `experiment_urls.json`, `popup-trigger-config.json`, `popup-rule-config.json`

## Security

- JWT (HS256, Hutool library), secret hardcoded as `"suguang"` in `AuthConstants`
- `JwtAuthenticationFilter` validates tokens, loads authorities from Redis, populates `UserContextHolder` (ThreadLocal via `TransmittableThreadLocal`)
- Request headers carry rich context: `token`, `lang`, `appVersion`, `deviceSys`, `deviceId`, `deepLink`, `deepIp`, `deepUserAgent`, `cloudfront-viewer-country`
- Guest login uses `deviceId` as password (BCrypt encoded)
- Third-party login (FB/Google/Apple) upgrades guest accounts — no server-side OAuth validation

## Key Conventions

- All responses wrapped in `Result<T>`
- DTO ↔ DAO conversion via MapStruct
- Distributed locks via Redisson
- Multi-language via `lang` header → `sys_lang` table (langId)
- Country via CloudFront `cloudfront-viewer-country` header
- Async processing via AWS SQS FIFO queues
- Cron jobs via XXL-Job (`TimerHandler`)
- `BaseDAO` base entity provides: `create_time`, `update_time`, `create_user`, `update_user`, `version` (optimistic lock), `deleted` (soft delete)
- `crowd_pack` is the core targeting dimension table, reused by push, popup, activity, payment, and ad strategy modules

## Important Files

| File | Purpose |
|---|---|
| `manage/.../config/SecurityConfig.java` | Spring Security config, endpoint whitelist |
| `manage/.../config/JwtAuthenticationFilter.java` | JWT validation filter |
| `manage/.../user/domain/service/AuthUserService.java` | Core auth logic (login, registration, third-party) |
| `manage/.../user/domain/context/UserContextHolder.java` | Request-scoped user context (ThreadLocal) |
| `manage/.../timer/impl/TimerServiceImpl.java` | Push scheduling (FCM + email) |
| `manage/.../sysconfig/domain/service/impl/FireBaseServiceImpl.java` | FCM push execution |
| `manage/.../sysconfig/domain/service/impl/AppPopupServiceImpl.java` | Popup rule evaluation |
| `manage/.../sysconfig/domain/service/impl/AppActivityPushServiceImpl.java` | Activity push entry evaluation |
| `manage/.../data/domain/repository/impl/EventLinkColorationRepositoryImpl.java` | Ad attribution coloration |
| `manage/.../experiment/domain/filter/ExperimentContextFilter.java` | A/B experiment context |
| `manage/.../aws/sqs/consumer/SqsColorationEventConsumer.java` | Attribution event consumer |

## Database

75+ tables across 10 domains. See `项目全量表结构与关系.md` for full schema. MySQL via MyBatis Plus + Druid. ClickHouse for analytics. Redis for caching/session.

## No Tests

Test classes exist but `@SpringBootTest` and `@Test` annotations are commented out. `mvn test` effectively runs nothing. Tests were likely run manually against a live test environment.

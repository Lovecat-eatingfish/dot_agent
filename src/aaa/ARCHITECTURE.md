# 项目架构文档

## 一、整体架构概览

本项目是一个基于 Spring Boot + Spring Cloud 的微服务架构系统，采用经典的三层架构设计，遵循模块化和高内聚低耦合原则。

## 二、模块结构

```
app/
├── dependencies/
├── framework/
│   └── common/
├── gateway/
├── datacenter/
└── manage/
    └── manage-service/
```

## 三、各模块职责

| 模块 | 职责 | 核心技术 |
|------|------|----------|
| dependencies | 统一依赖版本管理 | Maven BOM |
| framework/common | 通用工具、异常处理 | MyBatis Plus、Redis |
| gateway | 路由转发、请求过滤 | Spring Cloud Gateway |
| datacenter | Google Ads数据采集 | Google Ads API、ClickHouse |
| manage-service | 核心业务逻辑 | Spring Security、JWT |

## 四、技术栈

- Java 8
- Spring Boot 2.6.15
- Spring Cloud 2021.0.9
- Nacos 服务发现与配置
- MySQL + MyBatis Plus
- ClickHouse
- Redis (Redisson)

## 五、第三方集成

广告平台：Google Ads、Facebook Business、TikTok Business

支付网关：Stripe、PayPal、Alipay Global

云服务：AWS、火山引擎

## 六、部署架构

```
[客户端] → [Gateway(7999)] → [Nacos]
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        [manage-service]  [datacenter]    [其他服务]
              │               │
              ▼               ▼
           [MySQL]        [ClickHouse]
              │
              ▼
           [Redis]
```

## 七、端口配置

gateway: 7999, datacenter: 8200

---

文档生成时间: 2026-07-08
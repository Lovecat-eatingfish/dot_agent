# AB 实验系统详解

> 用途：理解项目的 A/B 测试系统实现，用于面试或开发参考

---

## 一、系统概述

这个项目的 AB 实验系统是一个**完整的实验平台**，支持：
- 多种实验能力（弹窗、支付、广告、定价等）
- 互斥实验和并行实验
- 基于 hash 的流量分配
- 用户分组和参数配置
- 实验上下文自动加载

---

## 二、核心表结构

### 1. experiment_config（实验配置表）

| 字段 | 说明 | 示例 |
|------|------|------|
| id | 主键 | 50 |
| experiment_name | 实验名称 | "弹窗样式AB测试" |
| experiment_type | 实验类型 | 0=互斥(MUTEX), 1=并行(ORTHOGONAL) |
| experiment_capability | 实验能力 | 1=弹窗推送, 2=三方支付链路, 3=广告策略... |
| experiment_status | 实验状态 | 0=未开始, 1=运行中, 2=已结束 |
| experiment_user_num | 目标样本量 | 10000 |
| crowd_type | 人群类型 | -1=不限, 0=指定人群包 |
| device_sys | 设备系统 | -1=通用, 0=iOS, 1=Android |
| link_channel | 归因渠道 | - |
| play_ids | 短剧ID列表 | - |
| countries | 国家列表 | - |
| start_time | 开始时间 | - |
| end_time | 结束时间 | - |
| user_num_sufficiency | 样本量是否充足 | false |

### 2. experiment_group_config（实验组配置表）

| 字段 | 说明 | 示例 |
|------|------|------|
| id | 主键 | 101 |
| experiment_group_name | 组名称 | "实验组A" |
| experiment_group_tag | 组标记 | 0=实验组, 1=对照组 |
| experiment_group_param | 组参数(JSON) | `{"enablePopupPush": true}` |
| experiment_id | 关联实验ID | 50 |
| participate_ratio | 流量比例 | 60 (60%) |
| participated_user_num | 已参与用户数 | 1234 |
| popup_rule_id | 关联弹窗规则ID | 200 |

### 3. experiment_participate_record（用户参与记录表）

| 字段 | 说明 | 示例 |
|------|------|------|
| id | 主键 | - |
| user_id | 用户ID | 800215 |
| experiment_id | 实验ID | 50 |
| experiment_group_id | 实验组ID | 101 |
| is_active | 是否活跃 | true |
| join_time | 加入时间 | 2026-07-28 10:00:00 |
| release_time | 释放时间 | - |

---

## 三、实验能力类型

```java
public enum ExperimentCapabilityEnum {
    COMMERCIALIZATION_ROLLBACK(0, "商业化功能回退"),  // 旧版组合实验
    POPUP_PUSH(1, "弹窗推送"),                        // 弹窗开关
    THIRD_PAY_LINK_TYPE(2, "三方支付链路"),            // 聚合/分步支付
    AD_STRATEGY(3, "广告策略"),                        // 广告策略配置
    ACTIVITY_AD_TASK(4, "活动页广告任务"),             // 活动页广告任务
    PAYMENT_TEMPLATE(5, "付费模板"),                   // 付费面板模板
    DRAMA_PRICE(6, "剧集价格"),                        // 单集定价
    AD_UNLOCK_UI(7, "广告解锁层UI"),                   // 广告解锁UI样式
    ACTIVITY_PUSH(8, "活动推送"),                      // 活动推送规则
    THIRD_PAY(9, "三方支付"),                          // 三方支付开关
    RECHARGE_TEMPLATE(10, "充值模板"),                 // 充值模板配置
}
```

---

## 四、实验类型

| 类型 | 值 | 说明 |
|------|-----|------|
| MUTEX | 0 | 互斥实验：用户只能进入一个互斥实验 |
| ORTHOGONAL | 1 | 并行实验：用户可同时进入多个并行实验 |

**互斥实验**：如果用户已经参与了互斥实验A，就不能再参与互斥实验B

**并行实验**：用户可以同时参与多个并行实验，互不影响

---

## 五、完整生命周期

### 1. 创建实验

```
运营在后台创建实验
    │
    ├── 实验名称: "弹窗样式AB测试"
    ├── 实验类型: 互斥(MUTEX)
    ├── 实验能力: 弹窗推送(POPUP_PUSH)
    ├── 目标样本量: 10000
    ├── 人群包: 注册7天内用户
    │
    ├── 实验组A (60%流量)
    │   └── 参数: {"enablePopupPush": true}
    │
    └── 对照组B (40%流量)
        └── 参数: {"enablePopupPush": false}
```

### 2. 启动实验

```
运营点击"启动"
    │
    ▼
experiment_status: NOT_START → RUNNING
    │
    ▼
开始接受用户流量
```

### 3. 用户请求触发实验分配

```
用户请求 /app/popup
    │
    ▼
ExperimentContextFilter
    │
    ├── 1. 检查URL是否需要实验
    │      └── 从Nacos读取 experiment.urls 配置
    │
    ├── 2. 加载用户已参与的实验
    │      └── 查 Redis 缓存 → 查数据库
    │
    ├── 3. 触发新实验分配
    │      └── ExperimentDistributeHelper.distributeExperiment()
    │
    └── 4. 设置实验上下文
           └── UserContextHolder.get().setUserRelevantExperiment()
```

### 4. 实验分配算法

```
用户ID: 800215
    │
    ▼
MurmurHash3 hash = hash("800215")
    │
    ▼
判断实验类型
    │
    ├── 互斥实验可用 + 并行实验可用
    │   └── hash % 2 == 0 → 互斥实验
    │   └── hash % 2 == 1 → 并行实验
    │
    ├── 只有互斥实验可用
    │   └── 进入互斥实验
    │
    └── 只有并行实验可用
        └── 进入并行实验
    │
    ▼
选择具体实验
    │
    └── hash("800215_0") % 实验数量 → 选中实验A
    │
    ▼
选择实验组
    │
    └── hash("800215_50") % 100
        │
        ├── 0-59 → 实验组A (60%)
        └── 60-99 → 对照组B (40%)
    │
    ▼
记录到 experiment_participate_record
    │
    ▼
缓存到 Redis (key: experiment:relation:user:800215)
```

### 5. 业务代码读取实验参数

```java
// 在业务代码中读取实验参数
PopupPushParam param = ExperimentParamHelper.getParam(PopupPushParam.class);
if (param != null && !param.getEnablePopupPush()) {
    // 实验配置了关闭弹窗
    return; // 不展示弹窗
}
```

### 6. 结束实验

```
运营点击"结束"
    │
    ▼
experiment_status: RUNNING → FINISHED
    │
    ▼
不再接受新用户
已参与的用户继续按实验参数执行
```

---

## 六、核心代码解析

### 1. ExperimentContextFilter — 实验上下文过滤器

```java
@Component
public class ExperimentContextFilter extends OncePerRequestFilter {
    
    @Override
    protected void doFilterInternal(HttpServletRequest request, 
                                    HttpServletResponse response, 
                                    FilterChain filterChain) {
        
        // 1. 检查URL是否需要实验
        String requestURI = request.getRequestURI();
        if (!experimentUrlProperties.matches(requestURI)) {
            filterChain.doFilter(request, response);
            return;
        }
        
        // 2. 获取当前用户
        LoginUser loginUser = UserContextHolder.get();
        if (loginUser == null || loginUser.getUserId() == null) {
            filterChain.doFilter(request, response);
            return;
        }
        
        Long userId = loginUser.getUserId();
        
        // 3. 加载用户已参与的实验
        Map<ExperimentGroupVO, ExperimentVO> userRelevantExperiment = 
            experimentConfigService.getUserRelevantExperiment(userId);
        
        // 4. 设置到上下文
        loginUser.setUserRelevantExperiment(userRelevantExperiment);
        
        // 5. 触发新实验分配
        experimentDistributeHelper.distributeExperiment(userId, null, null, false);
        
        filterChain.doFilter(request, response);
    }
}
```

### 2. ExperimentDistributeHelper — 实验分配核心

```java
@Component
public class ExperimentDistributeHelper {
    
    // MurmurHash3 哈希函数
    public long getHashCode(Long userId, Long experimentId) {
        String hashKeyStr;
        if (experimentId != null) {
            hashKeyStr = StrUtil.format("{}_{}", userId, experimentId);
        } else {
            hashKeyStr = String.valueOf(userId);
        }
        return Math.abs(Hashing.murmur3_128()
            .hashString(hashKeyStr, Charsets.UTF_8)
            .asLong());
    }
    
    // 分配实验
    public void distributeExperiment(Long userId, Long playId, 
                                     String seriesNum, boolean isColorationChange) {
        
        // 1. 染色延迟检查
        SysUserDAO user = userRepository.getById(userId);
        long delaySeconds = DateUtil.between(user.getCreateTime(), 
                                             colorationTime, DateUnit.SECOND);
        if (delaySeconds > colorationDelayTime) {
            return; // 超过延迟时间，不分
        }
        
        // 2. 过滤用户可参与的实验
        List<ExperimentConfig> eligibleExperiments = 
            experimentConfigService.filterUserCanParticipateExperiment(userId);
        
        if (CollUtil.isEmpty(eligibleExperiments)) {
            return;
        }
        
        // 3. 判断实验类型
        boolean hasMutex = eligibleExperiments.stream()
            .anyMatch(e -> e.getExperimentType() == 0);
        boolean hasOrthogonal = eligibleExperiments.stream()
            .anyMatch(e -> e.getExperimentType() == 1);
        
        // 4. 选择实验类型
        int pipelineType;
        if (hasMutex && hasOrthogonal) {
            // 两种都有，hash决定进哪个
            long userHashCode = getHashCode(userId, null);
            pipelineType = (int) (userHashCode % 2);
        } else if (hasMutex) {
            pipelineType = 0; // 互斥
        } else {
            pipelineType = 1; // 并行
        }
        
        // 5. 选择具体实验
        List<ExperimentConfig> targetExperiments = eligibleExperiments.stream()
            .filter(e -> e.getExperimentType() == pipelineType)
            .collect(Collectors.toList());
        
        if (CollUtil.isEmpty(targetExperiments)) {
            return;
        }
        
        // 互斥实验只能选一个
        if (pipelineType == 0) {
            long hash = getHashCode(userId, 0L);
            int index = (int) (hash % targetExperiments.size());
            ExperimentConfig selectedExperiment = targetExperiments.get(index);
            targetExperiments = Collections.singletonList(selectedExperiment);
        }
        
        // 6. 分配实验组
        for (ExperimentConfig experiment : targetExperiments) {
            distributeToGroup(userId, experiment);
        }
    }
    
    // 分配到具体实验组
    private void distributeToGroup(Long userId, ExperimentConfig experiment) {
        
        // 获取实验组列表
        List<ExperimentGroupConfig> groups = experimentGroupRepository.lambdaQuery()
            .eq(ExperimentGroupConfig::getExperimentId, experiment.getId())
            .list();
        
        // 构建流量分配列表
        // 例如: 参与比例 60/40 → 生成100个元素，60个A，40个B
        List<Long> groupIdList = new ArrayList<>();
        for (ExperimentGroupConfig group : groups) {
            for (int i = 0; i < group.getParticipateRatio(); i++) {
                groupIdList.add(group.getId());
            }
        }
        
        // hash取模分配
        long hashCode = getHashCode(userId, experiment.getId());
        int index = (int) (hashCode % groupIdList.size());
        Long selectedGroupId = groupIdList.get(index);
        
        // 记录分配结果
        handleUserBindExperiment(userId, experiment.getId(), selectedGroupId);
    }
}
```

### 3. ExperimentParamHelper — 实验参数读取

```java
@Component
public class ExperimentParamHelper {
    
    // 能力类型映射
    private static final Map<Integer, ExperimentCapabilityEnum> PARAM_CAPABILITY_MAP_V2 = new HashMap<>();
    static {
        PARAM_CAPABILITY_MAP_V2.put(1, ExperimentCapabilityEnum.POPUP_PUSH);
        PARAM_CAPABILITY_MAP_V2.put(2, ExperimentCapabilityEnum.THIRD_PAY_LINK_TYPE);
        PARAM_CAPABILITY_MAP_V2.put(3, ExperimentCapabilityEnum.AD_STRATEGY);
        // ... 更多映射
    }
    
    // 获取实验参数
    public static <T> T getParam(Class<T> paramClass) {
        // 1. 从映射表找到对应的能力类型
        ExperimentCapabilityEnum capability = PARAM_CAPABILITY_MAP_V2.get(paramClass);
        if (capability == null) {
            return null;
        }
        
        // 2. 从上下文找到用户的实验组
        ExperimentGroupVO group = findExperimentGroup(capability);
        if (group == null) {
            return null;
        }
        
        // 3. 反序列化参数
        return JSONObject.parseObject(group.getExperimentGroupParam(), paramClass);
    }
    
    // 查找用户的实验组
    private static ExperimentGroupVO findExperimentGroup(ExperimentCapabilityEnum capability) {
        Map<ExperimentGroupVO, ExperimentVO> experiments = 
            UserContextHolder.get().getUserRelevantExperiment();
        
        if (CollUtil.isEmpty(experiments)) {
            return null;
        }
        
        for (Map.Entry<ExperimentGroupVO, ExperimentVO> entry : experiments.entrySet()) {
            ExperimentVO experiment = entry.getValue();
            if (experiment.getExperimentCapability().equals(capability.getCode())) {
                return entry.getKey();
            }
        }
        
        return null;
    }
}
```

---

## 七、实验与业务系统集成

### 1. 弹窗系统集成

```java
// AppPopupServiceImpl.java

// 检查用户是否在AB实验中
Map<ExperimentGroupVO, ExperimentVO> userRelevantExperiment = 
    UserContextHolder.get().getUserRelevantExperiment();

if (CollUtil.isNotEmpty(userRelevantExperiment)) {
    appPopupVO.setIsInExperiment(true);
    
    // 检查是否在实验组
    for (ExperimentGroupVO group : userRelevantExperiment.keySet()) {
        if (group.getExperimentGroupTag().equals(0)) {
            appPopupVO.setIsInExperimentGroup(true);
            break;
        }
    }
}

// 读取实验参数
PopupPushParam param = ExperimentParamHelper.getParam(PopupPushParam.class);
if (param != null && !param.getEnablePopupPush()) {
    // 实验配置了关闭弹窗
    appPopupVO.setShowOldPopup(true);
    return appPopupVO;
}

// 实验组使用实验配置的弹窗规则
if (CollUtil.isNotEmpty(experimentPopupRuleList)) {
    // 直接使用实验配置的 popupRuleId
    // 跳过正常的人群包过滤
}
```

### 2. 支付系统集成

```java
// ThirdPayServiceImpl.java

// 读取充值模板实验参数
RechargeTemplateConfigParam param = 
    ExperimentParamHelper.getParam(RechargeTemplateConfigParam.class);

if (param != null) {
    // 使用实验配置的模板
    List<RechargeTemplateConfigParam.RuleDTO> rules = 
        dto.getChannel() == 1 ? param.getAndroidRules() : param.getIosRules();
    
    // 匹配入口规则
    // 构建响应
    buildResponseFromExperiment(dto, vo, matchedRule, countrySwitch);
    return vo;
}
```

### 3. 广告策略集成

```java
// AppAdStrategyServiceImpl.java

if (ExperimentParamHelper.isInExperiment()) {
    // 用户在实验中
    AdStrategyParam param = ExperimentParamHelper.getParam(AdStrategyParam.class);
    if (param != null) {
        // 使用实验配置的广告策略
        AdStrategyDAO adStrategyDAO = new AdStrategyDAO();
        BeanUtils.copyProperties(param, adStrategyDAO);
        return adStrategyDAO;
    }
}

// 不在实验中，使用数据库配置
return adStrategyRepository.getById(strategyId);
```

### 4. 活动推送集成

```java
// AppActivityPushServiceImpl.java

Map<ExperimentGroupVO, ExperimentVO> experiments = 
    ExperimentParamHelper.getExperiments();

if (CollUtil.isNotEmpty(experiments)) {
    experiments.forEach((group, experiment) -> {
        if (experiment.getExperimentCapability() == 
            ExperimentCapabilityEnum.ACTIVITY_PUSH.getCode()) {
            
            // 解析实验参数
            ActivityPushRuleParam param = JSONObject.parseObject(
                group.getExperimentGroupParam(), ActivityPushRuleParam.class);
            
            if (param != null && param.getIsActivity()) {
                // 使用实验配置的活动规则
                // 跳过数据库查询
            }
        }
    });
}
```

---

## 八、Hash 流量分配详解

### MurmurHash3 算法

```java
// 生成hash值
public long getHashCode(Long userId, Long experimentId) {
    String hashKeyStr;
    if (experimentId != null) {
        // userId_experimentId 格式
        hashKeyStr = StrUtil.format("{}_{}", userId, experimentId);
    } else {
        // 只用 userId
        hashKeyStr = String.valueOf(userId);
    }
    
    // MurmurHash3 128位哈希
    return Math.abs(Hashing.murmur3_128()
        .hashString(hashKeyStr, Charsets.UTF_8)
        .asLong());
}
```

### 流量分配示例

```
实验ID: 50
实验组A: participateRatio = 60
对照组B: participateRatio = 40

构建分配列表 (长度100):
[0-59]  = 实验组A (60个)
[60-99] = 对照组B (40个)

用户ID: 800215
hash = hash("800215_50") = 123456789
index = 123456789 % 100 = 89

89 >= 60 → 分配到对照组B
```

### 为什么用 MurmurHash3？

1. **均匀分布**：哈希值分布均匀，保证流量分配准确
2. **确定性**：同一个输入总是产生同一个输出，保证用户始终分到同一组
3. **高性能**：计算速度快，不影响请求响应时间
4. **低碰撞**：不同输入产生不同输出的概率很高

---

## 九、Redis 缓存策略

### 缓存结构

```
Key: experiment:relation:user:{userId}
Value: List<CacheExperimentDTO> [
    {experimentGroupId: 101, experimentId: 50},
    {experimentGroupId: 202, experimentId: 60}
]
TTL: 1天
```

### 缓存更新时机

1. **用户首次参与实验**：写入缓存
2. **用户再次请求**：读取缓存
3. **实验结束**：缓存过期自动清除

### 分布式锁

```
Key: lock:user:distribute:experiment:{userId}
用途: 防止同一用户并发分配实验
超时: 5秒
```

---

## 十、Nacos 配置

### experiment.urls

定义哪些 URL 需要触发实验过滤器：

```json
[
    "/app/popup",
    "/app/activity/entrances/query",
    "/app/goods/list",
    "/app/order/create"
]
```

### experiment.coloration_delay_time

染色延迟时间（秒），默认 60 秒。用户注册时间和染色时间差超过这个值，不分实验。

---

## 十一、管理后台接口

| 接口 | 方法 | 说明 |
|------|------|------|
| GET /experiment | getExperimentPage | 分页查询实验列表 |
| GET /experiment/{id} | getExperimentDetail | 查询实验详情（含实验组） |
| POST /experiment | addExperiment | 创建实验 |
| PUT /experiment | editExperiment | 编辑实验（未开始才能编辑） |
| PUT /experiment/{id}/start | startExperiment | 启动实验 |
| PUT /experiment/{id}/stop | stopExperiment | 结束实验 |
| PUT /experiment/relation/release/{groupId} | releaseExperimentGroupRelation | 释放实验组用户 |
| GET /experiment/control-group-param | getControlGroupParam | 获取对照组默认参数 |
| GET /experiment/capabilities | getCapabilities | 获取所有实验能力类型 |

---

## 十二、人群包冲突校验

创建实验时，系统会校验同能力类型的实验人群包不能重叠：

```java
// 校验逻辑
for (ExperimentConfig existingExperiment : existingExperiments) {
    if (existingExperiment.getExperimentCapability() == newExperiment.getExperimentCapability()) {
        // 同能力类型
        List<Long> existingCrowdIds = getCrowdIds(existingExperiment.getId());
        List<Long> newCrowdIds = getCrowdIds(newExperiment.getId());
        
        // 检查是否有交集
        boolean hasOverlap = existingCrowdIds.stream()
            .anyMatch(newCrowdIds::contains);
        
        if (hasOverlap) {
            throw new BusinessException("人群包有交集，不能创建实验");
        }
    }
}
```

**例外**：ACTIVITY_PUSH 类型允许人群包重叠，但推送规则不能冲突（同页面+同动作+同时长）。

---

## 十三、面试怎么说

**面试官："介绍一下你们的AB实验系统"**

> 我们的AB实验系统是一个完整的实验平台，支持多种业务场景的A/B测试。
>
> **核心设计**：
> 1. **两种实验类型**：互斥实验（用户只能参与一个）和并行实验（用户可同时参与多个）
> 2. **11种实验能力**：覆盖弹窗、支付、广告、定价等业务场景
> 3. **Hash流量分配**：使用MurmurHash3算法，基于userId和experimentId计算hash值，取模分配流量
> 4. **请求级上下文**：每个请求通过Filter自动加载用户的实验信息，业务代码直接读取
>
> **技术亮点**：
> 1. **确定性分配**：同一个用户始终分到同一组，保证实验一致性
> 2. **人群包隔离**：同能力类型的实验人群包不能重叠，避免相互干扰
> 3. **参数化配置**：实验参数以JSON存储，灵活支持各种业务场景
> 4. **缓存优化**：用户实验信息缓存到Redis，减少数据库查询

**面试官追问："怎么保证用户始终分到同一组？"**

> 使用MurmurHash3算法，输入是`userId_experimentId`，输出是确定的hash值。同一个输入 always 产生同一个输出，所以用户每次请求都会分到同一组。
>
> 例如：`hash("800215_50") = 123456789`，`123456789 % 100 = 89`，89 >= 60，所以用户800215在实验50中 always 分到对照组。

**面试官追问："互斥实验和并行实验有什么区别？"**

> **互斥实验**：用户只能参与一个互斥实验。如果用户已经参与了互斥实验A，就不能再参与互斥实验B。适用于实验之间会相互影响的场景。
>
> **并行实验**：用户可以同时参与多个并行实验。适用于实验之间互不影响的场景，比如同时测试弹窗样式和支付模板。
>
> 如果用户同时满足互斥和并行实验的条件，系统会用hash值决定进哪个：`hash(userId) % 2 == 0` 进互斥，`== 1` 进并行。

**面试官追问："实验参数是怎么传递给业务代码的？"**

> 实验参数存在`experiment_group_config`表的`experiment_group_param`字段，是JSON格式。
>
> 每个请求经过`ExperimentContextFilter`时，会把用户的实验信息加载到`UserContextHolder`。业务代码通过`ExperimentParamHelper.getParam(SomeParam.class)`读取参数，内部会：
> 1. 从映射表找到对应的能力类型
> 2. 从上下文找到用户的实验组
> 3. 反序列化JSON为Java对象
>
> 这样业务代码只需要一行就能获取实验参数，不需要关心实验分配的细节。

---

## 十四、代码文件索引

| 文件 | 说明 |
|------|------|
| ExperimentContextFilter.java | 实验上下文过滤器，每个请求自动加载实验信息 |
| ExperimentConfigServiceImpl.java | 实验配置服务，管理实验的增删改查 |
| ExperimentDistributeHelper.java | 实验分配核心，hash流量分配 |
| ExperimentParamHelper.java | 实验参数读取工具类 |
| ExperimentCapabilityEnum.java | 实验能力类型枚举 |
| ExperimentTypeEnum.java | 实验类型枚举（互斥/并行） |
| MurmurHash3HashStrategy.java | MurmurHash3哈希算法实现 |
| DefaultDistributeStrategy.java | 默认分配策略 |
| ExperimentController.java | 管理后台接口 |
| ExperimentConfigDAO.java | 实验配置实体 |
| ExperimentGroupConfigDAO.java | 实验组配置实体 |
| ExperimentParticipateRecordDAO.java | 用户参与记录实体 |

---

## 十五、用户参与多个实验的逻辑

### 两种实验类型

| 类型 | 说明 | 能参与几个 |
|------|------|-----------|
| **互斥实验 (MUTEX)** | 实验之间会相互影响 | **只能 1 个** |
| **并行实验 (ORTHOGONAL)** | 实验之间互不影响 | **可以多个** |

### 三种情况

#### 情况1：只有互斥实验

```
用户满足条件的实验：[实验A, 实验B, 实验C] (都是互斥)
    │
    ▼
只能选一个！
    │
    └── hash("800215_0") % 3 = 1
        → 选中 实验B
        → 用户只参与实验B
```

#### 情况2：只有并行实验

```
用户满足条件的实验：[实验D, 实验E, 实验F] (都是并行)
    │
    ▼
都加入！
    │
    ├── 实验D → hash("800215_60") % 100 = 55 → 实验组
    ├── 实验E → hash("800215_70") % 100 = 80 → 对照组
    └── 实验F → hash("800215_80") % 100 = 30 → 实验组
    │
    ▼
用户同时参与 3 个实验
```

#### 情况3：互斥 + 并行都有

```
用户满足条件的实验：
├── 互斥: [实验A, 实验B]
└── 并行: [实验D, 实验E]
    │
    ▼
先用 hash 决定进哪种 pipeline
    │
    hash("800215") = 987654321
    987654321 % 2 = 1
    → 1 = 并行 pipeline
    │
    ▼
进入并行 pipeline
    │
    ├── 实验D → 分配实验组
    └── 实验E → 分配实验组
    │
    ▼
用户参与 2 个并行实验
互斥实验A和B都不参与
```

### 完整流程图

```
用户 800215 请求
    │
    ▼
过滤可参与的实验
    │
    ├── 互斥实验: [实验A, 实验B]
    └── 并行实验: [实验D, 实验E]
    │
    ▼
判断进入哪种 pipeline
    │
    hash("800215") % 2
    │
    ├── 0 → 互斥 pipeline
    │       │
    │       ▼
    │   只能选一个互斥实验
    │       │
    │       hash("800215_0") % 2 = 1
    │       → 选中实验B
    │       │
    │       ▼
    │   分配实验B的实验组
    │       │
    │       ▼
    │   用户只参与实验B，不参与实验A、D、E
    │
    └── 1 → 并行 pipeline
            │
            ▼
        参与所有并行实验
            │
            ├── 实验D → 分配实验组
            └── 实验E → 分配实验组
            │
            ▼
        用户参与实验D和E
        不参与实验A和B
```

### 为什么互斥实验只能选一个？

**举个例子**：

```
实验A：测试弹窗样式 (互斥)
├── 实验组: 新样式弹窗
└── 对照组: 旧样式弹窗

实验B：测试弹窗频率 (互斥)
├── 实验组: 每天弹3次
└── 对照组: 每天弹1次
```

如果用户同时参与实验A和实验B：
- 实验A的实验组 + 实验B的实验组 → 新样式 + 每天3次
- 实验A的实验组 + 实验B的对照组 → 新样式 + 每天1次
- 实验A的对照组 + 实验B的实验组 → 旧样式 + 每天3次
- 实验A的对照组 + 实验B的对照组 → 旧样式 + 每天1次

**4 种组合，无法判断是哪个实验导致的效果差异！**

所以互斥实验只能选一个，保证实验结果的可解释性。

### 为什么并行实验可以多个？

**举个例子**：

```
实验C：测试弹窗样式 (并行)
├── 实验组: 新样式
└── 对照组: 旧样式

实验D：测试支付模板 (并行)
├── 实验组: 聚合支付
└── 对照组: 分步支付
```

弹窗样式和支付模板互不影响：
- 用户看到新样式弹窗，不影响支付行为
- 用户看到聚合支付，不影响弹窗点击

**可以同时测试，互不干扰！**

### 总结

| 场景 | 结果 |
|------|------|
| 只有互斥实验 | hash 选一个 |
| 只有并行实验 | 都加入 |
| 互斥 + 并行 | hash 决定进哪种 pipeline |

**核心原则**：
- **互斥实验**：保证实验结果可解释，只能选一个
- **并行实验**：实验之间互不影响，可以同时参与多个

---

## 十六、背诵清单

- [ ] 能说清楚3张核心表的作用
- [ ] 能说清楚11种实验能力
- [ ] 能说清楚互斥实验和并行实验的区别
- [ ] 能说清楚hash流量分配的原理
- [ ] 能说清楚实验上下文是怎么加载的
- [ ] 能说清楚业务代码怎么读取实验参数
- [ ] 能说清楚实验与弹窗/支付/广告的集成方式
- [ ] 能回答"怎么保证用户始终分到同一组"
- [ ] 能回答"为什么用MurmurHash3"
- [ ] 能回答"人群包冲突怎么处理"
- [ ] 能回答"用户如何选择加入哪个实验"
- [ ] 能回答"互斥实验为什么只能选一个"
- [ ] 能回答"并行实验为什么可以多个"

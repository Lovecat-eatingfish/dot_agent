## AgentHost:
Agent 统一入口。 这个里面主要是初始化这个全局的
1. 初始化这个 SessionManager：管理session 的获取 和创建的。 SessionPersistence： 处理session 和 trun文件持久化和加载的。 SessionManager 需要负责编排图
2. mcp host 和 skill host： mcp host 你需要预先和skill 建立连接 获取到所有的tool列表， skill 需要预先去 对应目录加载所有的skill放到内存中
还有hookRunner 启动的时候需要 加载所有的 hook 放到内存中
3. 初始化审批引擎


## SessionManager 和 SessionPersistence
前面这个管理者。  主要是管理这个获取这个session的  每一个session都有一个session id。 这个session id其实就是这个 长期记忆的这个文件名字。 文件名字，它是以这个时间戳命名的，逐步递增的。 每一个这个文件夹下面，它会有一个
session.json文件： 这个文件是主要存储这个。 agent对话期间所生成的所有 message（ai， user， tool）message 等。 在同一个文件夹下面还有一个。  turns文件夹 主要是 存放每一轮的产生的message 以及之前的message list， 同时还要存储git commit 的值，用来对话回滚
每一轮的数据都会新建一个trun_xxx.json文件存储。 这个git的目录就放在 session 目录下面多个会话共享就好了。

## mcp host 和 skill host 和 hook
都在 .dot/mcp 或者  .dot/skill  .dot/hook  里面 用一个json文件配置， 你需要在项目启动的时候 加载到内存中存储。 mcp 就直接加载这个json文件， skill 就去 对应目录加载所有的skill， hook 就去 对应目录加载所有的hook 文件
同时 mcp 需要去加载所有的tool 放到内存中， skill也是一样的 ， hook 直接初始化一个hookrunner对象 里面有多个hook 基础执行单元
- mcp 的工具的name 需要时mcp_servername_mcp工具的名字 命名避免重复
- skill 的名称统一添加skill_skill的名字

## 初始化审批引擎
这个先不做的， 但是需要预留位置， 主要就是工具调用前后


## agent 工作模式： 
1. plan： 只有读取 的tool
2. edit： bash 命令需要审批，其余随意执行
3. auto： 权限最大


## tool工具
添加 mcp_search工具 和 skill_search工具

### 执行tool
1. 判断函数的name  ， mcp_search工具 和 skill_search工具  需要特殊的逻辑执行
2. 其余的系统工具直接执行
3. 如果时mcp_开头 或者 skill_ 开头的 需要 特殊分析： 具体的就是。 如果是以m c p开头的。 你根据 它的这个函数的这个名称，去找这个 m c p工具的定义返回给这个大模型 如果是以这个skill_开头的。表示，人家想获取到这个skills的内容。 那你就把这个skills的数据全部加载给这个大模型，让它去进行判断即可。  


## coding agent 系统提示词
1. 在./dot/memory 目录下面有一个dot.md 这个文件时用户的行为偏好文件  提示词，动静分离的时候 出过这个系统提示词之外，你需要把这个。 文件的提示词加载到 静态的提示词里面进行拼接 
2. 动态提示词 就是m c p所有的工具名称及其描述，还有这个skills的名字，还有这个描述。   
3. 约定： 如果说大模型需要调用mcp_开头的，或者说是这个skill_开头的这个 skill。如果是以m c p开头的  先调用   mcp_search 这个函数调用 获取tool 的所欲描述信息，在进行调用。 如果说他是以这个skill开头的。那你就去获取这个skills下面的这个skill.md返回给这个大模型。 


## graph 编排
1. plan模型： 这个模型需要根据用户提出的这个问题。 去生成这个 步骤。 以及这个校验的这个步骤。
2. coding agent： 这个模型里面比较复杂。  需要进行提示词的动态，还有静态的构建。 还有系统工具的绑定  同时还需要有一个审批机制 在这个函数调用前后对其进行审核。 还需要有这个hook机制 在工具调用前后及其失败的情况下，调用这个hook机制
3. valid agent： 需要对这个 写代码的这个agent。 输出的内容，按照这个plan agent它生成的这个校验的这个步骤去校验。 你需要注意的一点是，这个校验的这个agent也是需要和这个大模型去交互的 
4. 还有一个。 finnaly agent： 它这个的话主要就是 把这一轮所产生的这个所有的数据要持久化到这个session文件里面。 还有吧，这一轮的数据放入到这个。 truns/trun__xxx.json 持久化这一轮的数据 并且需要把这一轮的这个嗯。 项目目录进行gate commit。 并且把这个commit的这个哈希值放入到这个。 trun_xx.json文件里面 。主要是方便日后的这个 嗯，上下班儿的一个回滚情况 


## 注意
1. 不要过度使用lang grahp 提供的功能。 只使用它的这个图的编排，以及这个任务流这一块的功能 不要使用它的打断机制 也不要使用它的这个chat point机制，以及这个回滚机制都不要使用它了 一定要自己去自定义。 
2. 常量尽量统一管理

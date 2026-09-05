"""
dot.core — 最底层共享内核

被所有层依赖（workflow / ai / agent / coding），自身不依赖任何 dot.* 模块。
只放跨层共享的基础原语：

- wire.py   ：WireModel 序列化基类（camelCase 别名、严格模式）
- cancel.py ：取消令牌 Protocol 与简单实现
"""

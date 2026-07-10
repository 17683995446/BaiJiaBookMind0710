"""book_digest —— 集百家之所长的拆书 Agent。

融合三大流派：
- 思维导图派（ebook-to-mindmap）：全书/章节双层导图
- 知识图谱派（SpineDigest）：三阶段流水线 + 概念关联图
- 技能化派（Cangjie Skill）：方法论提炼成可执行技能包
"""
from .config import AppConfig, BookType, OutputFormat

__version__ = "0.1.0"
__all__ = ["AppConfig", "BookType", "OutputFormat"]

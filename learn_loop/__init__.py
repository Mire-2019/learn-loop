"""learn-loop —— “让 agent 学会一个领域”的最小闭环。

模块划分：
  ingest.py  资料摄入（视频抽帧+时间戳烧入像素、字幕物理体检、文档读文本）
  notes.py   可核对笔记（每条结论都带来源引用与置信度）
  exam.py    跨条目综合考题 + --recheck 防回归
  gate.py    判据机检（通过/失败清单 + 退出码）
  evolve.py  把判据写回技能文件（写入后回读逐字节比对，不一致回滚）
  llm.py     模型调用层（urllib + 环境变量，无密钥时确定性 mock）
  cli.py     单入口 CLI
"""

__version__ = "0.1.0"
__all__ = ["__version__"]

# -*- coding: utf-8 -*-
"""项目根入口。

统一把应用暴露为 `app.main:app`，使下面两种启动方式都可用：

    .venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8101
    .venv\\Scripts\\python.exe -m uvicorn main:app     --port 8101
"""
from app.main import app  # noqa: F401

__all__ = ["app"]

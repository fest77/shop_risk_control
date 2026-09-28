# -*- coding: utf-8 -*-
"""模块 13 的进程内辅助模块（Spec §6 的 `app/tasks/`）。

目前只有 `health_probe`：组件探测的实现与状态清单。它被
`app/services/health_service.py` 在请求内调用（**没有后台定时任务**，
理由见 `health_probe` 的模块 docstring）。
"""

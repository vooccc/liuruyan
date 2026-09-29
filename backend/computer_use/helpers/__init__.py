"""Computer Use sidecar helpers.

按平台分文件，每个文件提供 `def main()` 入口（被 sidecar.py spawn 调用）。
所有 helper 共享 _common.py 里的 JSON 协议和 key normalize 逻辑。
"""

"""PTtools — PT 站点运维工具箱。

包含三块能力，全部由本进程的 Python 实现：

* CD2 守卫：CloudDrive2 上传任务秒传判定 / 超时拦截 / 备份重扫重试
* 115 监控：云下载超时清理
* PT 流量：qBittorrent / Transmission 上传流量监控与自动限速
  （``app/services/bt_client.py`` + ``bt_policy.py`` + ``bt_monitor.py``）
"""

__version__ = "2.1.0"
__all__ = ["__version__"]

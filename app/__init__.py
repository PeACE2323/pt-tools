"""PTtools — PT 站点运维工具箱。

包含三块能力：

* CD2 守卫：CloudDrive2 上传任务秒传判定 / 超时拦截 / 备份重扫重试
* 115 监控：云下载超时清理
* BT 流量：qBittorrent / Transmission 上传流量监控与自动限速
  （Go 子服务 bt-traffic-guard，经 /btg/* 反向代理接入）
"""

__version__ = "2.0.0"
__all__ = ["__version__"]

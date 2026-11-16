"""项目时间约定：DB/API 的 naive datetime 一律是北京墙钟（UTC+8，无夏令时）。

历史原因：所有生产者/消费者都用 `datetime.now()` 落库（本机为北京时区），
DuckDB TIMESTAMP 无时区。跨源（autopick 的 UTC ISO、Yahoo/GDELT 的 UTC）转换
必须显式走这里，不能依赖宿主机本地时区。
"""

from datetime import timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))

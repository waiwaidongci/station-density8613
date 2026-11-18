from enum import Enum
# 算法版本：结果与复算包均按此版本锁定；升级算法时递增并保留旧版实现，旧包仍按原版复算
ALGORITHM_VERSION='density-dbscan-v1'
class Role(str,Enum):
    admin='admin'; analyst='analyst'; viewer='viewer'
class RunState(str,Enum):
    queued='queued'; running='running'; completed='completed'; failed='failed'
class ReviewState(str,Enum):
    draft='draft'; approved='approved'

# -*- coding: utf-8 -*-
"""运行参数服务（模块 13 §4.1）：校验、原子保存、热更新与"生效方式"判定。

## 落点：DB 单文档，**不回灌 `app/config.py`**（任务书 §2③）

`app/config.py` 的 `validate()` 是**启动期 fail-fast**：缺 `MONGO_URL`/`JWT_SECRET`
就拒绝启动（COM-5002 / D25/D20）。运行参数是"管理员在页面上随时可改"的另一类
东西，它的落点是 `COLL_SYSTEM_CONFIG`（运行时读）。把 UI 值写回环境变量或让
`validate()` 依赖它们，会让"保存一个参数"变成"下次启动失败"——这是本模块最
容易犯、且只在重启时才暴露的错误。

**校验照样严**：越界即拒绝（`SYS-4001`）、短窗 ≥ 长窗即拒绝（`SYS-4002`），
**绝不静默截断**。截断会让用户以为自己设的值生效了，而"名单缓存 TTL 决定
新增黑名单的最长生效延迟"（AD-02）这种承诺是写进交付说明的。

## 原子性（Spec §5 / V-13-07）

六个参数在**同一个文档**里，一次 `update_one` 全部生效——不存在"TTL 改了、
超时没改"的半个配置。校验在写入之前**全部**做完，任一字段不合法即整次拒绝。

## 生效方式必须如实返回（BR-13-02）

- **立即生效**：窗口时长与容量（热更新实例属性）、名单缓存 TTL（改 TTL **并
  立即清空现有缓存**，BR-13-04）、决策链路超时（改 03/05 读的那个模块级值）。
- **需重启**：指标桶粒度（BR-13-07，涉及定时聚合任务周期的重建）。
- 热更新某一项失败时**不假装成功**：把该项挪进 `requires_restart` 并附
  `SYS-5004` 告知（Spec §5：「配置已保存，但部分模块未生效，建议重启」）。

## 审计恰好一条（D41 / BR-13-03）

写入成功后写**恰好一条** `config.update`，含 `before`/`after`（`strict=True`）；
审计写不进去则把文档回滚成本次之前的样子并抛 `SYS-5001`——"宁可不做，
不可无痕地做"（与模块 06 的 `AuditRollbackError` 同一处置）。

**无变化时不写审计、不推进版本**：`before == after` 的记录只会污染审计链，
而版本号是决策快照的锚（BR-13-08），无意义的点击不该把它推高——这与 06 的
「启停用到同一状态则不动」（BR-06-03 末句 / V-06-20）是同一条裁定。
"""
from __future__ import annotations

from typing import Any, Optional

from app.constants import (
    METRIC_BUCKET_GRANULARITIES,
    RUNTIME_CONFIG_DEFAULTS,
    RUNTIME_CONFIG_RANGES,
    RUNTIME_CONFIG_ID,
)
from app.errors import (
    AppError,
    ConfigSaveFailedError,
    ConfigValueOutOfRangeError,
    ModelEngineUnavailableError,
    WindowOrderInvalidError,
)
from app.logging import get_logger
from app.repos.config_repo import ConfigRepo
from app.repos.model_config_repo import DEFAULT_CONFIG_ID, ModelConfigRepo
from app.schemas.system_schema import ENGINE_TYPES, FUSE_MODES
from app.services.audit_service import audit
from app.utils.timeutil import now_ms

log = get_logger("shop_risk_control.system.config")

#: 乐观锁重试次数（决策 D42）：并发下"读到后被别人改了"是正常竞争，
#: 重读重验再写即可；连续冲突才如实报 `SYS-5001`（不做后写覆盖）
_VERSION_RETRY = 3

#: 初始/默认配置版本（= 04 的 `WINDOW_CONFIG_VERSION`）。首次保存后推进为 `w2`
INITIAL_CONFIG_VERSION = 1

AUDIT_ACTION = "config.update"


def format_config_version(num: Any) -> str:
    """整数版本 -> 对外字符串（`1` → `"w1"`）。

    `w1` 与 04 的 `WINDOW_CONFIG_VERSION` **是同一个字符串**：快照里的
    `window_config.config_version` 读的就是它（BR-13-08 的"随快照留存"）。
    """
    try:
        return f"w{int(num)}"
    except (TypeError, ValueError):
        return f"w{INITIAL_CONFIG_VERSION}"


def config_version_num(version: Any) -> int:
    """对外字符串 -> 整数（`"w3"` → 3；非法值回落成 1）。"""
    text = str(version or "").lstrip("wW")
    return int(text) if text.isdigit() else INITIAL_CONFIG_VERSION


def default_fields() -> dict[str, Any]:
    """六个运行参数的默认值（Spec 13 §2.1 的"默认"列）。"""
    return dict(RUNTIME_CONFIG_DEFAULTS)


def _normalize(doc: Optional[dict]) -> dict[str, Any]:
    """把库文档归一成"六个字段 + 版本 + 生效时间"（缺字段用默认值补齐）。

    为什么要归一：文档是**跨版本演进**的（将来加一个参数时，老文档里没有这一
    列）。若直接 `doc["x"]`，一次增量升级会让 `GET /system/config` 500——
    而它正是运维用来发现问题的那一个接口。
    """
    fields = default_fields()
    if doc:
        for key in fields:
            if doc.get(key) is not None:
                fields[key] = doc[key]
    version = int((doc or {}).get("config_version") or INITIAL_CONFIG_VERSION)
    fields["config_version"] = version
    fields["config_version_str"] = format_config_version(version)
    fields["effective_at"] = int((doc or {}).get("effective_at") or 0)
    fields["updated_at"] = (doc or {}).get("updated_at")
    fields["updated_by"] = (doc or {}).get("updated_by")
    return fields


class ConfigService:
    """运行参数的读、校验、原子保存与热更新。"""

    def __init__(self, repo: ConfigRepo):
        self.repo = repo

    # ---------------- 读 ----------------
    async def get_config(self) -> dict:
        """`GET /system/config` 的 `data`（未被保存过时按代码默认值返回）。"""
        current = _normalize(await self.repo.find())
        return {
            **{k: current[k] for k in RUNTIME_CONFIG_DEFAULTS},
            "effective_at": current["effective_at"] or now_ms(),
            "config_version": current["config_version_str"],
            "config_version_num": current["config_version"],
            "ranges": {k: [lo, hi] for k, (lo, hi) in RUNTIME_CONFIG_RANGES.items()},
            "defaults": default_fields(),
            "updated_by": current["updated_by"],
        }

    # ---------------- 校验 ----------------
    @staticmethod
    def validate_patch(patch: dict, base: dict) -> None:
        """校验本次提交的子集（`SYS-4001` / `SYS-4002`），失败即抛、不写入任何值。

        ## 判定顺序：**先判"短窗 < 长窗"，再判单字段取值域**

        顺序是被验收项钉死的：Spec §7 的 V-13-02 用「短窗 2000、长窗 100」提交，
        它同时违反两条规则（2000 也超出 1~1440），而期望的错误码是 `SYS-4002`。
        若先判取值域，这个用例永远拿不到契约点名的码——与模块 06 把
        「`score` 越界必须给 `CFG-4004` 而不是 `COM-4001`」是同一条取舍：
        **契约点名的码优先于"更自然"的码**。
        """
        if not patch:
            raise AppError("COM-4001", "没有需要保存的参数", 422)

        unknown = sorted(set(patch) - set(RUNTIME_CONFIG_DEFAULTS))
        if unknown:
            # 模型层已经挡住了未知键（Pydantic 默认忽略额外字段），这里兜住
            # "服务层被直接调用"的路径——静默忽略未知参数会让调用方以为它生效了
            raise AppError("COM-4001", f"未知参数：{'、'.join(unknown)}", 422)

        # ① 跨字段关系（SYS-4002）：用**合并后**的值判定，这样"只改短窗"
        #    也能发现它已经越过了长窗
        short = patch.get("short_window_min", base.get("short_window_min"))
        long = patch.get("long_window_min", base.get("long_window_min"))
        if short is not None and long is not None and int(short) >= int(long):
            raise WindowOrderInvalidError(short, long)

        # ② 单字段取值域（SYS-4001）
        for field, (low, high) in RUNTIME_CONFIG_RANGES.items():
            if field not in patch:
                continue
            value = patch[field]
            # `bool` 必须显式排除：`isinstance(True, int)` 为真，
            # 否则 `short_window_min: true` 会被当成 1 分钟写进库
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigValueOutOfRangeError(field, low, high, value)
            if not (low <= value <= high):
                raise ConfigValueOutOfRangeError(field, low, high, value)

        # ③ 枚举取值（Spec §5 未为该场景分配 `SYS-` 码，按通用参数错误处理）
        if "metric_bucket_granularity" in patch:
            value = str(patch["metric_bucket_granularity"])
            if value not in METRIC_BUCKET_GRANULARITIES:
                raise ConfigValueOutOfRangeError(
                    "metric_bucket_granularity",
                    "/".join(METRIC_BUCKET_GRANULARITIES), "", value,
                )

    # ---------------- 保存 ----------------
    async def save_config(
        self,
        patch: dict,
        *,
        operator: str,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> dict:
        """`PUT /system/config` 的 `data`（Spec §3.2）。

        顺序固定：**读 → 校验 → 条件写 → 审计 → 热更新**。
        审计在热更新**之前**：热更新一旦执行，决策侧就已经按新参数工作了，
        此时若审计落不下去再回滚数据库，运行中的进程与库里的值会不一致。
        而"审计失败 → 回滚库"之后进程仍按旧参数运行，两者自洽。
        """
        base = _normalize(await self.repo.find())
        self.validate_patch(patch, base)

        merged = {**{k: base[k] for k in RUNTIME_CONFIG_DEFAULTS}, **patch}
        changed = {
            key: {"before": base[key], "after": merged[key]}
            for key in RUNTIME_CONFIG_DEFAULTS
            if key in patch and base[key] != merged[key]
        }
        if not changed:
            # 无实际变化：不写库、不写审计、不推进版本（见模块 docstring）
            return {
                "config_version": base["config_version_str"],
                "config_version_num": base["config_version"],
                "effective_at": base["effective_at"] or now_ms(),
                "applied": [],
                "requires_restart": [],
                "notices": [],
                "changed": {},
            }

        ts = now_ms()
        before_doc, after_doc = await self._persist(patch, operator=operator, ts=ts)
        version_str = format_config_version(after_doc.get("config_version"))

        try:
            await audit(
                actor=operator, actor_role=actor_role, action=AUDIT_ACTION,
                target_type="config", target_id=RUNTIME_CONFIG_ID,
                before={"config_version": format_config_version(before_doc.get("config_version"))
                        if before_doc else None,
                        **{k: changed[k]["before"] for k in changed}},
                after={"config_version": version_str,
                       **{k: changed[k]["after"] for k in changed}},
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            rolled = False
            if before_doc is None:
                # 首次保存：本次是插入，回滚 = 删掉这一行（回到"按默认值运行"）
                rolled = await self.repo.delete_document()
            else:
                rolled = await self.repo.restore(
                    int(after_doc.get("config_version") or 0), before_doc
                )
            log.error("config.update 审计写入失败，已回滚运行参数：%s", e)
            hint = "" if rolled else "；且回滚亦失败，请人工核对运行参数"
            raise ConfigSaveFailedError(f"{type(e).__name__}{hint}") from e

        applied, requires_restart, notices = self.apply_runtime_config(
            self._fields_of(after_doc),
            changed_fields=set(changed),
            config_version=version_str,
        )
        return {
            "config_version": version_str,
            "config_version_num": int(after_doc.get("config_version") or INITIAL_CONFIG_VERSION),
            "effective_at": int(after_doc.get("effective_at") or ts),
            "applied": applied,
            "requires_restart": requires_restart,
            "notices": notices,
            "changed": changed,
        }

    async def _persist(self, patch: dict, *, operator: str, ts: int) -> tuple[Optional[dict], dict]:
        """条件写入运行参数，返回 `(写入前的文档或 None, 写入后的文档)`。

        `config_version` 在这里占用**乐观锁**的角色（决策 D42）：更新条件带上
        "我读到的那个版本号"，匹配 0 条即说明在我读之后有人改过 —— 此时重读、
        **重新校验**（新的长窗可能让"短窗 < 长窗"不再成立）再写；连续冲突才
        如实报 `SYS-5001`。不做后写覆盖：后写覆盖会让前一个管理员的改动
        消失且无人知晓。
        """
        last_reason = ""
        for _ in range(_VERSION_RETRY):
            current = await self.repo.find()
            if current is None:
                # **首次保存也要 +1**：初始状态（从未保存过）就是 `w1`（= 04 的
                # `WINDOW_CONFIG_VERSION`），所以这一次保存落库的版本是 `w2`。
                # 写成 1 会让"每次保存 +1"（BR-13-08）在第一次就失效——
                # 表现为"改了参数、版本号却没动"，而那正是可追溯性的锚。
                doc = {
                    **default_fields(), **patch,
                    "config_version": INITIAL_CONFIG_VERSION + 1,
                    "effective_at": ts, "updated_at": ts, "updated_by": operator,
                }
                if await self.repo.insert_if_absent(doc):
                    return None, doc
                last_reason = "首次保存时被并发创建"
                continue

            base_fields = self._fields_of(current)
            # 并发下 current 已变：必须重新校验（否则可能写出 short >= long 的组合）
            self.validate_patch(patch, base_fields)
            version = int(current.get("config_version") or INITIAL_CONFIG_VERSION)
            update = {
                **patch,
                "config_version": version + 1,
                "effective_at": ts, "updated_at": ts, "updated_by": operator,
            }
            if await self.repo.update_with_version(version, update) == 1:
                return current, {**current, **update}
            last_reason = f"版本 {version} 已被他人推进"
        raise ConfigSaveFailedError(f"运行参数并发冲突（{last_reason}），请重试")

    @staticmethod
    def _fields_of(doc: Optional[dict]) -> dict[str, Any]:
        return _normalize(doc)

    # ---------------- 热更新 ----------------
    def apply_runtime_config(
        self,
        fields: dict[str, Any],
        *,
        changed_fields: Optional[set[str]] = None,
        config_version: Optional[str] = None,
    ) -> tuple[list[str], list[str], list[dict]]:
        """把参数下发到各消费者，返回 `(applied, requires_restart, notices)`。

        `changed_fields` 为 `None` 表示"全部下发"（启动期装载：进程刚起来，
        每个消费者都还是代码默认值，必须按库里的值整体对齐）。
        逐项吞掉异常并降级为 `requires_restart` + `SYS-5004`：一个消费者装配
        失败不该让整次保存失败（配置**已经**落库并留痕），但也**绝不假装**它生效了。
        """
        target = set(RUNTIME_CONFIG_DEFAULTS) if changed_fields is None else set(changed_fields)
        applied: list[str] = []
        restart: list[str] = []
        notices: list[dict] = []

        def _notice(message: str, **extra: Any) -> None:
            notices.append({"code": "SYS-5004", "message": message, **extra})

        # ---- ① 窗口时长与容量（BR-13-05/06，立即生效） ----
        window_fields = {"short_window_min", "long_window_min", "window_capacity"}
        if target & window_fields:
            try:
                from app.services.feature_service import get_feature_service

                window = get_feature_service().window
                result = window.apply_config(
                    short_window_min=(fields["short_window_min"]
                                      if "short_window_min" in target else None),
                    long_window_min=(fields["long_window_min"]
                                     if "long_window_min" in target else None),
                    capacity=(fields["window_capacity"]
                              if "window_capacity" in target else None),
                    # 版本号总是下发：任何参数保存都推进版本（BR-13-08），
                    # 而快照读的就是它
                    config_version=config_version,
                )
                applied += [
                    name for name in ("short_window_min", "long_window_min", "window_capacity")
                    if name in target and name in result["changed"]
                ]
                if result.get("dropped_entries"):
                    _notice(
                        f"窗口容量已下调，立即丢弃了 {result['dropped_entries']} 条历史窗口数据"
                        f"（特征值可能偏低，下次 sweep 起按新上限运行）",
                        dropped_entries=result["dropped_entries"],
                    )
            except Exception as e:  # noqa: BLE001 - 单项失败不得让整次保存失败
                log.error("窗口参数热更新失败：%s: %s", type(e).__name__, e)
                restart += sorted(target & window_fields)
                _notice(f"窗口参数未生效（{type(e).__name__}），需重启后按新值运行",
                        fields=sorted(target & window_fields))

        # ---- ② 名单缓存 TTL（BR-13-04：改 TTL **并立即清空现有缓存**） ----
        if "list_cache_ttl_sec" in target:
            try:
                from app.engine.list_filter import LIST_CACHE
                from app.services.list_service import invalidate_list_cache

                LIST_CACHE.ttl_sec = float(fields["list_cache_ttl_sec"])
                # 只改 TTL 不清缓存是不够的：已缓存的**未命中**结果会按旧 TTL
                # 继续有效，而"新增黑名单的最长生效延迟"承诺的正是这个 TTL
                invalidate_list_cache()
                applied.append("list_cache_ttl_sec")
            except Exception as e:  # noqa: BLE001
                log.error("名单缓存 TTL 热更新失败：%s: %s", type(e).__name__, e)
                restart.append("list_cache_ttl_sec")
                _notice(f"名单缓存 TTL 未生效（{type(e).__name__}），需重启",
                        fields=["list_cache_ttl_sec"])

        # ---- ③ 决策链路超时（BR-13-01 的取值域；模块 03/05 在调用时读它） ----
        if "decision_timeout_ms" in target:
            try:
                from app.engine import decision as decision_engine
                from app.services import event_service

                timeout_ms = int(fields["decision_timeout_ms"])
                # 这三个名字是 03/05 的**运行期取值点**：`event_service` 用
                # `TIMEOUT_SEC` 包 `asyncio.wait_for`，`decision` 用
                # `SLOW_THRESHOLD_MS` 判慢决策。两者都在函数体内按模块全局取值，
                # 因此改这里就对下一次决策生效（不需要重启）。
                event_service.TIMEOUT_SEC = timeout_ms / 1000.0
                # `DECISION_TIMEOUT_MS` 是 03 自己的模块级名字（`from ... import`），
                # 它只用在超时告警的**文案**里（"决策链路超时（>200ms）"）。
                # 不一并改就会出现"实际 500ms 超时、提示却说 200ms"的自相矛盾，
                # 而这条提示正是运维判断"要不要调大超时"的依据。
                event_service.DECISION_TIMEOUT_MS = timeout_ms
                decision_engine.SLOW_THRESHOLD_MS = timeout_ms
                applied.append("decision_timeout_ms")
            except Exception as e:  # noqa: BLE001
                log.error("决策超时热更新失败：%s: %s", type(e).__name__, e)
                restart.append("decision_timeout_ms")
                _notice(f"决策链路超时未生效（{type(e).__name__}），需重启",
                        fields=["decision_timeout_ms"])

        # ---- ④ 迟到判定的长窗（03 的 `LATE_WINDOW_MS` 与长窗是同一个口径） ----
        if "long_window_min" in target and "long_window_min" in applied:
            try:
                from app.services import event_service

                event_service.LATE_WINDOW_MS = int(fields["long_window_min"]) * 60_000
            except Exception as e:  # noqa: BLE001 - 它只影响"迟到"标记，不影响决策
                log.warning("迟到窗口热更新失败（不影响决策）：%s: %s", type(e).__name__, e)

        # ---- ⑤ 指标桶粒度（BR-13-07：**需重启**） ----
        if "metric_bucket_granularity" in target:
            restart.append("metric_bucket_granularity")
            value = str(fields["metric_bucket_granularity"])
            if value == "1d":
                # 11 的 `set_write_granularity` 明确拒绝 1d 作为**写入**基础粒度
                # （BR-11-03：一天一个桶会让 24h 趋势只剩一个点）。这是"保存了
                # 但重启后也不会生效"的情形，必须**如实**告诉用户，而不是等
                # 他重启之后发现没变
                _notice(
                    "指标桶粒度 1d 不能被模块 11 用作写入基础粒度（BR-11-03："
                    "24h 趋势会只剩一个点），重启后仍将按 1m 运行；如需按天查看，"
                    "请改用 1h 桶的 rollup 结果",
                    fields=["metric_bucket_granularity"],
                )

        for name in sorted(target):
            if name in RUNTIME_CONFIG_DEFAULTS and name not in applied and name not in restart:
                # 兜底：任何没有被上面任何分支处理的参数都必须**出现在两个数组之一**，
                # 否则就是"静默保存"（BR-13-02 明令禁止）
                restart.append(name)
        return applied, restart, notices

    # ---------------- 启动期装载 ----------------
    async def load_and_apply(self) -> dict:
        """启动期把库里的运行参数装载到各消费者（**不影响启动成败**）。

        为什么需要它：需要重启才生效的参数（指标桶粒度）必须"下一次启动时真的
        按库里的值运行"，否则那个参数永远只是一个摆设。库不可达时只记告警——
        启动期"缺库"已经有自己的处置（`db.bootstrap` 降级启动、`/health` 上报
        down），不该由运行参数把进程拦下来。
        """
        from app.services.metric_service import set_write_granularity

        try:
            current = _normalize(await self.repo.find())
        except AppError as e:
            log.warning("启动期读取运行参数失败（按代码默认值运行）：%s", e)
            return {"loaded": False, "detail": e.message}

        fields = {k: current[k] for k in RUNTIME_CONFIG_DEFAULTS}
        version = current["config_version_str"]
        applied, restart, notices = self.apply_runtime_config(
            fields, changed_fields=None, config_version=version
        )
        granularity = set_write_granularity(str(fields["metric_bucket_granularity"]))
        log.info(
            "运行参数已装载（版本 %s）：立即生效 %s；重启生效 %s；实际写入粒度 %s",
            version, applied or "（无）", restart or "（无）", granularity,
        )
        return {
            "loaded": True,
            "config_version": version,
            "applied": applied,
            "requires_restart": restart,
            "notices": notices,
            "write_granularity": granularity,
        }

    # ---------------- 测试用复位 ----------------
    # 说明：复位函数刻意是**模块级**函数（见文件末尾的 `reset_runtime_state`），
    # 不是本类的方法——它的调用方是 `tests/conftest.prep_db`（逐用例复位进程内
    # 状态），不经过任何服务实例；做成类方法会让夹具不得不先造一个 repo 出来，
    # 而"为了复位而连库"本身就是多余的 IO。


def build_config_service(db: Any) -> ConfigService:
    """装配（供依赖注入与测试复用）。"""
    return ConfigService(ConfigRepo(db))


def reset_runtime_state() -> None:
    """把运行参数在各消费者上的**进程内**状态复位成代码默认值。

    这是测试夹具的复位点（`tests/conftest.prep_db` 逐用例调用），存在的理由与
    `rule_service.reset_create_idempotency()` / `decision.reset_stats()` 完全相同：
    运行参数会被写进**进程级**的 `FeatureWindow`、`event_service.TIMEOUT_SEC`、
    `LIST_CACHE.ttl_sec`、`decision.SLOW_THRESHOLD_MS`。不复位就会出现
    "上一个用例把决策超时改成 5000ms，下一个用例的超时降级断言莫名其妙不触发"
    ——而失败会出现在**与模块 13 无关**的用例上，极难定位（与 conftest 里
    `_reset_feature_injections` 踩过的是同一类坑）。

    只动进程内状态，**不碰数据库**：库里那份配置由 `prep_db` 清集合来复位。
    """
    from app.engine import decision as decision_engine
    from app.engine.feature_window import WINDOW_CONFIG_VERSION
    from app.engine.list_filter import LIST_CACHE
    from app.services import event_service
    from app.services.feature_service import get_feature_service

    defaults = default_fields()
    window = get_feature_service().window
    window.apply_config(
        short_window_min=defaults["short_window_min"],
        long_window_min=defaults["long_window_min"],
        capacity=defaults["window_capacity"],
        config_version=WINDOW_CONFIG_VERSION,
    )
    LIST_CACHE.ttl_sec = float(defaults["list_cache_ttl_sec"])
    LIST_CACHE.clear()
    event_service.TIMEOUT_SEC = int(defaults["decision_timeout_ms"]) / 1000.0
    # 03 自己的模块级名字（只用于超时告警文案）也要复位，理由见 apply_runtime_config
    event_service.DECISION_TIMEOUT_MS = int(defaults["decision_timeout_ms"])
    event_service.LATE_WINDOW_MS = int(defaults["long_window_min"]) * 60_000
    decision_engine.SLOW_THRESHOLD_MS = int(defaults["decision_timeout_ms"])


async def load_and_apply() -> dict:
    """启动期装载运行参数的模块级入口（供 `app.main` 的 lifespan 调用）。

    **永不抛异常**：`config.validate()` 已经负责了"配置缺失就拒绝启动"（COM-5002），
    运行参数不属于那一类——库暂时不可达时按代码默认值运行即可，进程该起来
    还是得起来（否则 `/health` 也没了，运维无法区分"进程没起来"与"库挂了"）。
    """
    from app import db

    try:
        return await ConfigService(ConfigRepo(db.get_db())).load_and_apply()
    except Exception as e:  # noqa: BLE001 - 启动期任何意外都只降级，不阻断
        log.warning("装载运行参数失败（按代码默认值运行）：%s: %s", type(e).__name__, e)
        return {"loaded": False, "detail": f"{type(e).__name__}: {e}"}


# ============================================================
# 决策引擎配置（E20 `model_configs`，**架构预留** AD-09 / G-01）
# ============================================================
#: 模型引擎未接入的如实说明。§2.4 要求页面用**橙色警示条**显著标注它，
#: 因此这句话由后端下发（前端不得自己编一段），文案与 Spec §2.4 的引用块一致。
MODEL_ENGINE_NOTE = (
    "⚠ 模型引擎（ModelEngine）当前为空实现，恒返回 None，final_score = rule_score。"
    "是否实现、用什么算法属悬空点 G-01，需与老师确认后填写。本页仅做架构预留展示。"
)

ENGINE_AUDIT_ACTION = "engine.config.update"

#: E20 的默认记录（BR-13-21：`engine_type=rule`、`fuse_mode=rule_first`、
#: `rule_weight=1.0`、`model_weight=0.0`）
ENGINE_DEFAULTS: dict[str, Any] = {
    "engine_type": "rule",
    "fuse_mode": "rule_first",
    "weights": {"rule": 1.0, "model": 0.0},
    "model_name": None,
    "status": "enabled",
}

#: 只有 `rule` 是"有实现"的引擎类型（AD-09：`ModelEngine` 恒返回 None）
AVAILABLE_ENGINE_TYPES: tuple[str, ...] = ("rule",)


class EngineConfigService:
    """`GET/PUT /system/engine-config` 的业务规则（§3.5 / §4.3）。

    ## 为什么不做成"可开关但无效果"的假开关（BR-13-22/23）

    AD-09 已裁定模型引擎是空实现：`app/protocols.py` 的 `NullModelEngine` 恒返回
    `None`，05 的仲裁只读 `rule_score`，因此 `final_score ≡ rule_score`。
    本节因此**只接受 `engine_type=rule`**，其余一律 `SYS-4003`「模型引擎尚未实现」。
    允许"保存成功"会让页面显示"模型引擎已启用"，而决策链路里没有任何一行代码
    会因此改变——那是最坏的一类缺陷：让人相信一件没有发生的事。

    同理，`fuse_mode` 可以保存（§2.4 有下拉框），但响应里**明确标注**
    `fuse_mode_effective=false`：在 `model_weight=0` 时三种融合方式都等于
    `rule_score`，它不是一只能在起作用的开关。
    """

    def __init__(self, repo: ModelConfigRepo):
        self.repo = repo

    async def get_config(self) -> dict:
        doc = await self.repo.find_default()
        return self._render(doc)

    async def save_config(
        self,
        payload: Any,
        *,
        operator: str,
        actor_role: str = "",
        ip: Optional[str] = None,
        ua: Optional[str] = None,
    ) -> dict:
        engine_type = str(payload.engine_type or "").strip()
        if engine_type not in ENGINE_TYPES:
            raise AppError(
                "COM-4001", f"engine_type 仅支持 {'/'.join(ENGINE_TYPES)}，收到：{engine_type!r}", 422
            )
        if engine_type not in AVAILABLE_ENGINE_TYPES:
            # BR-13-22：明确拒绝，**不改配置**
            raise ModelEngineUnavailableError(engine_type)

        fuse_mode = getattr(payload, "fuse_mode", None)
        if fuse_mode is not None and fuse_mode not in FUSE_MODES:
            raise AppError(
                "COM-4001", f"fuse_mode 仅支持 {'/'.join(FUSE_MODES)}，收到：{fuse_mode!r}", 422
            )

        current_doc = await self.repo.find_default()
        before = self._render(current_doc)
        target = {
            **ENGINE_DEFAULTS,
            "engine_type": engine_type,
            # `engine_type=rule` 时权重**只读**（BR-13-24）：不接受客户端传权重，
            # 也不按引擎类型"顺手"改成别的值
            "fuse_mode": fuse_mode or before["fuse_mode"],
        }
        changed = {
            key: {"before": before[key], "after": target[key]}
            for key in ("engine_type", "fuse_mode")
            if before[key] != target[key]
        }
        if not changed:
            return {"changed": False, "config": before}

        ts = now_ms()
        patch = {**target, "updated_at": ts, "updated_by": operator}
        if current_doc is None:
            await self.repo.insert_if_absent(patch)
        else:
            matched = await self.repo.update(patch)
            if matched == 0:
                raise ConfigSaveFailedError("决策引擎配置写入失败（文档不存在）")

        after = self._render({**(current_doc or {}), **patch})
        try:
            await audit(
                actor=operator, actor_role=actor_role, action=ENGINE_AUDIT_ACTION,
                target_type="config", target_id=DEFAULT_CONFIG_ID,
                # 只记**真正变化**的字段，且 before/after 都是标量（与 12 的审计
                # 详情页的展开对比格式一致：`field: before -> after`）
                before={k: v["before"] for k, v in changed.items()},
                after={k: v["after"] for k, v in changed.items()},
                ip=ip, ua=ua, strict=True,
            )
        except AppError as e:
            rolled = (await self.repo.restore(current_doc)) if current_doc else False
            log.error("engine.config.update 审计写入失败，已回滚决策引擎配置：%s", e)
            hint = "" if rolled else "；且回滚亦失败，请人工核对 E20"
            raise ConfigSaveFailedError(f"{type(e).__name__}{hint}") from e

        return {"changed": True, "config": after}

    @staticmethod
    def _render(doc: Optional[dict]) -> dict:
        """库文档 -> §3.5 的响应结构（未灌种子时给代码默认值）。

        `model_available` **恒为 false**：它不是"读一个字段"，而是 AD-09 的
        事实——05 的仲裁里根本没有消费 `model_score` 的分支
        （`app/engine/decision.py` 的 `model_score` 恒为 `None`）。
        同时把**实际装配**的模型引擎类名一并下发（D45：默认装配体现真实组件），
        让"为什么是 false"在接口层面可核对。
        """
        from app.protocols import get_components

        weights = (doc or {}).get("weights") or ENGINE_DEFAULTS["weights"]
        rule_weight = float(weights.get("rule", 1.0))
        model_weight = float(weights.get("model", 0.0))
        from app.engine.decision import ENGINE_VERSION

        return {
            "engine_type": str((doc or {}).get("engine_type") or ENGINE_DEFAULTS["engine_type"]),
            "fuse_mode": str((doc or {}).get("fuse_mode") or ENGINE_DEFAULTS["fuse_mode"]),
            "rule_weight": rule_weight,
            "model_weight": model_weight,
            "model_available": False,
            "status": str((doc or {}).get("status") or ENGINE_DEFAULTS["status"]),
            "engine_version": ENGINE_VERSION,
            "model_engine": type(get_components().model_engine).__name__,
            # 在 model_weight=0 时三种融合方式都等于 rule_score：它不是一只
            # "在起作用"的开关，必须如实标注（否则页面会让人以为它在生效）
            "fuse_mode_effective": model_weight > 0,
            "note": MODEL_ENGINE_NOTE,
            "reference": "G-01",
            "updated_at": (doc or {}).get("updated_at"),
            "updated_by": (doc or {}).get("updated_by"),
            "defaults": {k: v for k, v in ENGINE_DEFAULTS.items()},
        }


def build_engine_config_service(db: Any) -> EngineConfigService:
    """装配（供依赖注入与测试复用）。"""
    return EngineConfigService(ModelConfigRepo(db))


__all__ = [
    "AUDIT_ACTION", "AVAILABLE_ENGINE_TYPES", "ConfigService", "ENGINE_AUDIT_ACTION",
    "ENGINE_DEFAULTS", "EngineConfigService", "INITIAL_CONFIG_VERSION",
    "MODEL_ENGINE_NOTE", "build_config_service", "build_engine_config_service",
    "config_version_num", "default_fields", "format_config_version", "load_and_apply",
    "reset_runtime_state",
]

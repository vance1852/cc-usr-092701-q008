# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

## 患者、评估与诊疗计划

- `POST /patients` 建立诊所内患者档案；外部编号在诊所范围内唯一。
- `GET /patients/{patient_id}` 返回最小档案，不返回联系方式密文。
- `POST /patients/{patient_id}/merge` 以两个版本号和书面原因将重复档案标记为合并，并指向保留档案。
- `POST /patients/{patient_id}/assessments` 新建评估草稿；`POST /assessments/{assessment_id}/sign` 由临床岗位签署。
- `POST /patients/{patient_id}/consents` 创建更高版本的授权；`POST /consents/{consent_id}/withdraw` 撤回授权。
- `POST /patients/{patient_id}/plans` 建立计划，医美和体重管理计划必须引用当前对应授权。
- `POST /plans/{plan_id}/{propose|activate|pause|resume|complete|cancel}` 以 `expected_version` 执行带版本保护的状态转换。
- `GET /patients/{patient_id}/weight-series` 返回按观察时间排序的测量值，不生成诊断或治疗建议。

评估签署后不可覆盖。就诊病历由章节组成，签署需要主诉、评估和计划三部分；签署后的补充内容成为新版本，原始文字仍保留。

## 预约、随访与计划节点

创建预约须提供 `Idempotency-Key`，有责任人的预约不能与未结束时段重叠。临时占位到期后由 `POST /appointments/{id}/book` 拒绝确认，过期占位可通过服务方法按限额释放。预约状态按占位、确认、到诊、服务、完成推进；开始服务时产生就诊记录。

随访和计划节点支持领取租约、版本校验、幂等创建、延期和完整处置历史。旧领取者不能以过期令牌提交结果；重新领取不会删除前次领取事件。

## 患者联系偏好与门诊通知待办

患者按用途选择允许的联系渠道、静默时段和自己的时区；服务只生成内部待办并由工作人员人工触达，不接入任何短信或消息平台。

- `PUT`/`POST /patients/{patient_id}/contact-preferences` 登记或更新偏好（更新须带 `expected_version`），每次变更保留不可变修订记录；`GET` 读取当前版本，`GET /patients/{patient_id}/contact-preferences/revisions` 查看修订历史。
- `channels` 按用途给出渠道列表，用途为 `appointment_change`（预约变化）和 `followup_reminder`（随访到期）；渠道为 `phone`、`sms`、`message`、`in_person`。`quiet_hours` 为可选的 `{start,end}`（患者本地 `HH:MM`），结束不晚于开始表示跨午夜；跨午夜窗口与夏令时缺口/重叠日均按 `timezone` 的 IANA 时区规则解释。
- `POST /notifications/sweep` 扫描已到期随访和随访类计划节点生成内部任务，可重复执行；`POST /notifications` 供工作人员手工补录任务。预约确认（`book`）和取消（`cancel`）在状态转换时自动生成 `appointment_change` 任务。每个任务带 `source_type`、`source_id`、`source_event`，同一来源事件只建立一次任务（无需额外幂等键）。
- `GET /notifications?state=open|queued|claimed|blocked|...` 是工作人员的待联系名单；`POST /notifications/claim` 按到期时间领取（带租约），`POST /notifications/{id}/attempts` 登记 `delivered` 或 `failed` 结果。失败任务回到队列等待重试，每次尝试都保留在任务历史中；`POST /notifications/{id}/resolve-manual` 登记最终人工处理结论，`POST /notifications/{id}/reopen` 在患者重新授权后解除阻止。`GET /notifications/{id}` 与 `GET /notifications/{id}/history` 返回任务快照与完整事件序列。

任务创建时快照授权版本（用途 `followup_contact`）、偏好版本、渠道和静默解释后的最早可联系时间。缺少授权、偏好或该用途无可用渠道时任务以明确 `blocked_reason` 进入 `blocked`，不出现在待联系名单。撤回 `followup_contact` 授权会立即阻止该患者所有尚未领取的排队任务；已领取任务在提交结果前重新核对患者状态、当前渠道和授权版本，授权被撤回或有新版本时拒绝登记并阻止任务。上述操作需要 `followup:manage` 岗位权限（医生、护理、运营协调员）。

## 诊所耗材

- `POST /products` 登记耗材；`POST /products/{product_id}/lots` 按批号入库。
- `POST /stock/reserve` 依据失效日期按先到期先出分批预留，需要 `Idempotency-Key`。
- `POST /stock/{reservation_id}/consume` 记录患者使用；`release` 释放尚未使用的数量。
- `POST /stock/{lot_id}/quarantine`、`recall` 或 `release-quarantine` 记录批次处置及受影响预留。
- `GET /stock/lots` 查看可用数量；`GET /stock/{lot_id}/history` 查看批次流水。

入库、占用、释放与患者使用均进入不可变流水。存在不足时整笔预留回滚；被隔离、召回或在诊所本地日期已过期的批次不能继续使用。

## 不良事件与数据使用

护理人员可报告事件或患者安全关注项；临床岗位复核并记录处置，诊所负责人可作废就诊记录。`GET /audit/verify` 校验诊所哈希链，`GET /audit/diagnostics` 汇报需人工核对的一致性问题，不自动修改业务状态。

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
- 通知任务：排队 → 领取 → 已送达；失败回到排队重试。阻止（缺授权/偏好/渠道、授权撤回或版本过期）和人工处理是独立终态，阻止任务在重新授权后可解除。每次领取、尝试、阻止、解除和人工处理均保留不可变事件。

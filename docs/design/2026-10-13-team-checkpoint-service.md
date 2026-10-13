# TeamCheckpointService: explicit snapshot ownership

Manager remains the Team facade, routing commands to MessageRouter,
TaskBoardService, ApprovalService, ExecutionService and WorkerSupervisor.
To avoid the manager also owning state serialization, export_state and
import_state now delegate to TeamCheckpointService.

Service ownership:
- TeamStore: durable team and work-item data
- ApprovalService: approval lifecycle
- WorkerSupervisor: active worker handles and shutdown
- MessageRouter: message delivery/status
- ExecutionService: teammate model/tool invocation
- TeamCheckpointService: session snapshot projection and recovery integration
- TeamManager: public façade and orchestration between owners

Restore must not start workers; resume_workers remains the explicit Manager
command. This avoids duplicate dispatch during a restarted session's restore.

Existing checkpoint envelope keys and legacy pending approval backfill are
unchanged. Core tests cover session snapshot, parallel requeue, approval
persistence, idempotent imports and delegation.

Further work: move the worker inbox workflow to its own application object
and replace ad-hoc persistence with transactions. Do not rearrange Manager
worker ownership without race-condition tests.

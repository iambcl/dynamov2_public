import time
from typing import Any, Dict
from src.message_queue import MessageQueueServer
from dynamov2.database.db_helper import db_helper
from dynamov2.logger.logger import CustomLogger

host = MessageQueueServer()
sleep = True
logger = CustomLogger("Queue Manager", logfile_name="stage3:rabbitmq_queues")

def _determine_stage3_status(processed_payload: Dict[str, Any]) -> str:
    """Infer the Stage 3 status from the processed payload."""
    explicit_status = processed_payload.get("stage3_processing_status")
    if explicit_status:
        return explicit_status

    success_flag = processed_payload.get("success")
    if success_flag is False:
        return "failed"

    update_traffic_parameters: Dict[str, Any] = processed_payload.get("update_traffic_parameters", {})
    failure_reason = update_traffic_parameters.get("failure_reason")
    if failure_reason:
        return "failed"

    one_minute_check = update_traffic_parameters.get("one_minute_check")
    if one_minute_check is False:
        return "failed"

    return "complete"


while True:
    processed_message = host.consume_result()
    if processed_message:
        update_github_repository = processed_message['update_github_repository']
        update_traffic_parameters = processed_message['update_traffic_parameters']
        update_github_repository.setdefault(
            "stage3_processing_status",
            _determine_stage3_status(processed_message),
        )
        db_helper.update_github_repository(**update_github_repository)
        db_helper.update_traffic_parameters(**update_traffic_parameters)
        logger.info("Updated processed message.")
        sleep = False
    if host.require_queue_length() == 0:
        rows = db_helper.get_unchecked_repositories(stage=3, limit=5)
        if rows:
            for row in rows:
                message = {
                    'id': row.id,
                    'name': row.name,
                    'docker_compose_filepath': row.cleaned_docker_compose_filepath
                }
                if len(message['docker_compose_filepath']) == 0:
                    db_helper.update_traffic_parameters(repository_id=row.id, one_minute_check=False, failure_reason="No docker compose file present")
                    continue
                try:
                    host.publish_task(message)
                    logger.info(f"Added {message} to queue.")
                except Exception as exc:
                    logger.exception(f"Failed to enqueue repository {row.id}: {exc}")
                    db_helper.update_github_repository(
                        repository_id=row.id,
                        stage3_processing_status="pending",
                    )
        sleep = False
    if sleep:
        time.sleep(10)
    sleep = True
    

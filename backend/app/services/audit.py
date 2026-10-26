from sqlalchemy.orm import Session

from ..models import AuditLog


def audit(db: Session, user_id: int | None, action: str, entity: str, entity_id: int | None, **details) -> None:
    db.add(AuditLog(user_id=user_id, action=action, entity=entity, entity_id=entity_id, details=details))

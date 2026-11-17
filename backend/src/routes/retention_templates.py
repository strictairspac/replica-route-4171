from fastapi import APIRouter, Depends
from src.config.database import db_session
from src.utils.security import current, writer
from src.schemas.retention import RetentionTemplateInput
from src.services import retention_templates as service
router = APIRouter()


@router.get('/retention_templates')
def listing(db=Depends(db_session), user=Depends(current)):
    return service.listing(db)


@router.post('/retention_templates', status_code=201)
def create(data: RetentionTemplateInput, db=Depends(db_session), user=Depends(writer)):
    return service.create(db, data, user)


@router.put('/retention_templates/{ident}')
def update(ident: int, data: RetentionTemplateInput,
           db=Depends(db_session), user=Depends(writer)):
    return service.update(db, ident, data, user)

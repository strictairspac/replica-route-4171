import json
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from src.config.database import db_session
from src.utils.security import current, writer
from src.schemas.retention import RetentionDrillInput
from src.services import retention_drills as service
router = APIRouter()


@router.get('/retention_drills')
def listing(db=Depends(db_session), user=Depends(current)):
    return service.listing(db)


@router.post('/retention_drills', status_code=201)
def create(data: RetentionDrillInput, db=Depends(db_session), user=Depends(writer)):
    return service.create(db, data, user)


@router.get('/retention_drills/{ident}')
def detail(ident: int, db=Depends(db_session), user=Depends(current)):
    return service.detail(db, ident)


@router.get('/retention_drills/{ident}/export')
def export(ident: int, db=Depends(db_session), user=Depends(current)):
    return Response(
        json.dumps(service.detail(db, ident), ensure_ascii=False, indent=2),
        media_type='application/json',
        headers={'Content-Disposition': f'attachment; filename="retention-{ident}.json"'},
    )

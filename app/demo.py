"""Populate only the explicitly selected demo database; repeat runs are idempotent."""
import os
from .db import init_db, get_db, BASE_DIR
from . import ingest


def main():
    if not os.environ.get('EHR_DB_PATH'):
        raise RuntimeError('请通过 start.ps1 -Demo 运行，避免修改正式数据库')
    init_db()
    with get_db() as db:
        patient_id = db.execute('SELECT id FROM patients WHERE patient_no=?', ('P2026001',)).fetchone()[0]
        for source, external in [('hospital','H001'),('examination','E001'),('followup','C001')]:
            ingest.bind(db,source,external,patient_id)
            filename=source+'.csv'
            result=ingest.import_csv(db,source,filename,(BASE_DIR/'examples'/filename).read_bytes())
            if result['failed']:
                raise RuntimeError(str(result))
            print(source, result)


if __name__ == '__main__': main()

"""Owns disposable browser-test projects; never edits the source project."""
import sys,uuid,os,shutil
from server.store import get_project,save_project,project_dir,list_projects,connection

if sys.argv[1]=='create':
    pid=sys.argv[2] if len(sys.argv)>2 else next(p['id'] for p in list_projects() if p['status']=='ready' and p['source']=='youtube' and all(n.string or n.technique=='percussion' for n in get_project(p['id']).notes))
    p=get_project(pid);source=project_dir(pid);p.id=uuid.uuid4().hex;p.title='브라우저 검증용 복제';p.revision=0;p.source='file';p.metadata['browser_test']=True
    target=project_dir(p.id)
    for name in ['source.mp4','audio.wav','poster.jpg']:
        if (source/name).exists():os.link(source/name,target/name)
    save_project(p);print(p.id)
elif sys.argv[1]=='delete':
    p=get_project(sys.argv[2])
    if not p.metadata.get('browser_test'):raise RuntimeError('Only disposable test projects can be deleted here')
    with connection() as db:
        db.execute('DELETE FROM projects WHERE id=?',(p.id,));db.execute('DELETE FROM history WHERE project_id=?',(p.id,))
    shutil.rmtree(project_dir(p.id))

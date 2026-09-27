"""Owns disposable browser-test projects; never edits the source project."""
import sys,uuid,os,subprocess
from server.store import delete_project,get_project,save_project,project_dir,list_projects

if sys.argv[1]=='create':
    # A synced score has no transcribed notes to edit, so it cannot stand in for one.
    pid=sys.argv[2] if len(sys.argv)>2 else next((p['id'] for p in list_projects() if p['status']=='ready' and p['source']=='youtube' and not p['synced'] and p['note_count'] and all(n.string or n.technique=='percussion' for n in get_project(p['id']).notes)),None)
    if pid is None:sys.exit('브라우저 검사에 쓸 프로젝트가 없습니다. 앱에서 YouTube 영상을 하나 분석해 미정 운지가 없는 상태로 만든 뒤 다시 실행하거나, TABSIGHT_E2E_PROJECT에 프로젝트 ID를 지정해 주세요.')
    p=get_project(pid);source=project_dir(pid);p.id=uuid.uuid4().hex;p.title='브라우저 검증용 복제';p.revision=0;p.source='file';p.metadata['browser_test']=True
    target=project_dir(p.id)
    for name in ['source.mp4','audio.wav','poster.jpg']:
        if (source/name).exists():os.link(source/name,target/name)
    save_project(p);print(p.id)
elif sys.argv[1]=='clip':
    # A short excerpt of the copy's video, which test:e2e imports as a new song to check the settings step.
    subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-ss','20','-t','12','-i',str(project_dir(sys.argv[2])/'source.mp4'),'-c','copy',sys.argv[3]],check=True,timeout=60)
elif sys.argv[1]=='delete':
    try:p=get_project(sys.argv[2])
    except KeyError:sys.exit(0)  # test:e2e already deleted its copy through the sidebar.
    if not p.metadata.get('browser_test'):raise RuntimeError('Only disposable test projects can be deleted here')
    delete_project(p.id)

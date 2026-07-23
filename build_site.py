#!/usr/bin/env python3
"""기존 board_<리뷰어>.html 4개를 카테고리별 파일 + 인덱스 메뉴 + Supabase 저장 사이트로 변환.

- 입력:  SRC_DIR/board_*.html  (각 파일은 <script id="data"> 로 전체 payload 임베드)
- 출력:  OUT_DIR/index.html + OUT_DIR/data/<rslug>/<cslug>.html
- 저장:  각 보드의 Apps Script sync -> Supabase upsert(reviews) 로 교체
- 복원:  로드 시 syncPull이 서버에서 본인 리뷰를 받아 로컬이 빈 샘플만 채움(로컬 우선)
"""
import json, re, glob, os, collections, html as _html, shutil, math

OUT_DIR = os.path.dirname(os.path.abspath(__file__))  # build_site.py가 있는 저장소(하드코딩 경로 제거)
# 슬림 빌드 소스(base64 제거, ~11MB×4)를 저장소에 커밋 — 외부 휘발성 Downloads/temp-2 의존 제거.
SRC_DIR = os.path.join(OUT_DIR, "board_source")

SUPABASE_URL = "https://qnhwcwsizommxuqfpalo.supabase.co"
SUPABASE_KEY = "sb_publishable_Ss861mkQyztCl_CAtAbvmQ_ecG0fZDa"

# 이미지: base64 임베드 대신 공개 S3 URL 참조로 대체 — 레포 ~870MB→~50MB, 외부 폴더
# (llm-api-research/data/images) 의존 제거. candidate id -> s3Key 맵은 image_urls.json에 커밋.
S3_BASE = "https://alwayz-assets.s3.amazonaws.com/"
_img_map_path = os.path.join(OUT_DIR, "image_urls.json")
IMG_MAP = json.load(open(_img_map_path, encoding="utf-8")) if os.path.exists(_img_map_path) else {}

# --- 추가 검수(LLM 재라벨 검증) 소스 --------------------------------------------
# board_source/verify_<검수자>.jsonl = 검수자별 배정된 검증 샘플(top10 후보 + LLM 선택 + 근거
# + 공개 이미지 URL). phaseId를 카테고리로 삼아 VERIFY_CHUNK 단위로 카드를 분할해, 기존
# c1~c11과 '동일한' 카테고리 카드 파이프라인(카드/진행률바/대시보드)으로 흡수한다.
# 화면 라벨은 순수 카테고리명(예: "2.2.1 (뷰티)") — 단 진행률 집계키(grp)는 기존 카테고리와
# 겹치면 안 되므로 grp_key(verify::<phase>::<chunk>)로 분리한다. 라벨은 '추가 검수' 섹션 헤더로 구분.
VERIFY_CHUNK = 200
VERIFY_SECTION = "추가 검수"
VERIFY_RUNS = [{"run_id": "llm-relabel-gemini3flash-top10", "provider": "google",
                "model": "gemini-3-flash-preview", "prompt": "relabel/top10-select", "think": "low"}]

def _vgrpkey(phase, chunk):
    """진행률 집계용 내부 키 — 기존 카테고리명과 절대 충돌하지 않도록 접두사 부여."""
    return f"verify::{phase}::{chunk}"

# 진행 순서(소프트). 표시 라벨은 데이터 원본 형식. 매칭 키는 공백 제거 정규화.
# 순서: 기존 운영 6개 -> 신규 카테고리 3개 -> 기존 카테고리 추가분('… 신규') 2개(맨 아래).
TARGET_ORDER = [
    "2.1.1",
    "2.1.1 (출산/유아동)",
    "2.1.1.2 (출산/유아동)",
    "2.1.2 (출산/유아동)",
    "2.2.1 (뷰티)",
    "2.2.1 (영양제)",
    "2.1.3 (신선)",
    "4.1.2 (가공식품)",
    "4.1.3 (펫)",
    "2.1.1 (출산/유아동) 신규",
    "2.1.1.2 (출산/유아동) 신규",
]

def norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))

NORM_ORDER = [norm(c) for c in TARGET_ORDER]
CAT_SLUG = {norm(c): f"c{i+1}" for i, c in enumerate(TARGET_ORDER)}
CAT_LABEL = {norm(c): c for c in TARGET_ORDER}

# 검수자 -> rN 슬러그 고정 순서(배포 URL 안정성). 목록 밖 이름은 뒤로.
REVIEWER_ORDER = ["김민지", "유다연", "이지나", "조승현"]

# --- 카테고리 확장/제외 + 신규 섹션 분리 --------------------------------------
# 제외: board 미노출(요청: 스포츠/레저·가구·패션렌즈). DB 리뷰는 별개로 보존됨.
EXCLUDE_LABELS = {"2.2.2 (스포츠/레져)", "4.1.1 가구", "3.1.2 (패션렌즈_의류/잡화)"}
EXCLUDE_NORM = {norm(x) for x in EXCLUDE_LABELS}
# 기존 운영 카테고리: 기준선(현재 board)에 없던 샘플은 '<라벨> 신규' 섹션으로 분리.
EXISTING_LABELS = {
    "2.1.1", "2.1.1 (출산/유아동)", "2.1.1.2 (출산/유아동)",
    "2.1.2 (출산/유아동)", "2.2.1 (뷰티)", "2.2.1 (영양제)",
}
EXISTING_NORM = {norm(x) for x in EXISTING_LABELS}
NEW_SUFFIX = " 신규"

# 기준선: 확장 시점의 현재 board 샘플 id(검수자명 -> set). '기존 vs 신규' 판별 기준.
BASELINE = {}
_baseline_path = os.path.join(OUT_DIR, "baseline_samples.json")
if os.path.exists(_baseline_path):
    BASELINE = {k: set(v) for k, v in json.load(open(_baseline_path, encoding="utf-8")).items()}

# --- 재분배: 이지나 하차분을 나머지 3인에게 파트너 제약 균형 배분 --------------
# 이 데이터는 2인 중복검수 설계(아이템당 정확히 2명 배정, 동일 id=동일 상품).
# 이지나 하차 카테고리를 나머지 3인에게 재배분하되, 각 아이템을 '아직 그 아이템을
# 안 가진' 2명 중 부하 최소자에게 배정한다 — 기존 파트너(동일 아이템 보유자)를 피해
# id 충돌 없이 2인 중복검수를 유지한다. 정렬 id 기준이라 재빌드마다 결정론적.
#   FULL: 이지나 전량 이관(잔여 0).  HALF: 이지나가 절반 유지(정렬 id 짝수 index)·나머지 이관.
REDIST_FROM = "이지나"
REDIST_TO = ["김민지", "유다연", "조승현"]
REDIST_FULL_LABELS = [
    "4.1.2 (가공식품)", "4.1.3 (펫)",
    "2.1.1 (출산/유아동) 신규", "2.1.1.2 (출산/유아동) 신규",
]
REDIST_HALF_LABELS = ["2.1.3 (신선)"]
REDIST_FULL_NORM = {norm(x) for x in REDIST_FULL_LABELS}
REDIST_HALF_NORM = {norm(x) for x in REDIST_HALF_LABELS}
REDIST_NORM = REDIST_FULL_NORM | REDIST_HALF_NORM


def section_label(sample, reviewer):
    """샘플이 들어갈 board 섹션 라벨. 제외 대상은 None."""
    g = str(sample.get("group", "") or "")
    gn = norm(g)
    if gn in EXCLUDE_NORM:
        return None
    if gn in EXISTING_NORM:
        base = BASELINE.get(reviewer, set())
        return g if sample.get("id") in base else g + NEW_SUFFIX
    return g  # 신규 카테고리 등 — TARGET_ORDER에 없으면 이후 단계에서 드롭

DATA_RE = re.compile(r'(<script type="application/json" id="data">)(.*?)(</script>)', re.S)

# --- sync JS 교체 (Apps Script -> Supabase) --------------------------------
SYNCPUSH_RE = re.compile(
    r"function syncPush\(id\)\{if\(!SYNC\|\|!reviewer\)return;.*?"
    r"\.catch\(function\(\)\{setSyncStat\('저장 실패\(로컬 보관\)','bad'\);\}\);\}",
    re.S,
)
NEW_SYNCPUSH = (
    # vrec: Supabase upsert 레코드 조립(syncPush/pushList/autoFlush 공통). 샘플 메타(grp/name/gt)는
    # 이 보드에 있는 샘플일 때만 포함 — 다른 보드 전체저장이 기존 행 카테고리를 ''로 덮어써
    # 진행률에서 빠지는 것 방지. grp는 grp_key(추가 검수 카드) 우선(기존 카테고리명과 집계 분리).
    "function vrec(id){var p=noteParts(id),s=sampById[String(id)];\n"
    "    var rec={reviewer:reviewer,sample_id:String(id),tags:p.tags.join('|'),url:p.url,note:p.body,gt_candidates:(p.gtPicks||[]).join('|')};\n"
    "    if(s){rec.grp=(s.grp_key||s.group||'');rec.name=s.name||'';rec.gt=s.gt||'';}return rec;}\n"
    "function syncPush(id){if(!SB_URL||!SB_KEY||!reviewer)return;var rec=vrec(id);\n"
    "    fetch(SB_URL+'/rest/v1/reviews?on_conflict=reviewer,sample_id',{method:'POST',"
    "headers:{apikey:SB_KEY,Authorization:'Bearer '+SB_KEY,'Content-Type':'application/json',"
    "Prefer:'resolution=merge-duplicates,return=minimal'},body:JSON.stringify(rec)})\n"
    # 저장 성공 시 서버 시그니처 갱신 -> 미동기화 배지가 즉시 '모두 저장됨'으로.
    "      .then(function(r){if(r.ok){dirty=false;setSyncStat('저장 '+nowHM(),'ok');updateTagSum();"
    "SERVERSIG[String(id)]=locSig(id);renderUnsynced();}"
    "else{setSyncStat('저장 실패(로컬 보관)','bad');renderUnsynced();}})\n"
    "      .catch(function(){setSyncStat('저장 실패(로컬 보관)','bad');renderUnsynced();});}\n"
    # syncPull: 로드 시 서버(reviews)에서 본인 리뷰를 받아 로컬이 빈 샘플만 복원.
    # 로컬 입력이 항상 우선(덮어쓰기 없음) — 브라우저 교체/초기화 시 자동 복구용.
    "function syncPull(){if(!SB_URL||!SB_KEY||!reviewer)return;var acc=[],PAGE=1000,PAR=8;\n"
    "    var base=SB_URL+'/rest/v1/reviews?select=sample_id,tags,url,note,gt_candidates&reviewer=eq.'+encodeURIComponent(reviewer)+'&order=sample_id.asc&limit='+PAGE+'&offset=';\n"
    "    function gp(off){return fetch(base+off,{headers:{apikey:SB_KEY,Authorization:'Bearer '+SB_KEY}}).then(function(r){return r.ok?r.json():[];});}\n"
    # 페이지를 PAR개씩 병렬 요청(wave) — 순차 왕복 제거. 모든 페이지가 꽉 찼으면 다음 wave.
    "    function wave(start){var rq=[],i;for(i=0;i<PAR;i++)rq.push(gp((start+i)*PAGE));\n"
    "      return Promise.all(rq).then(function(ps){var full=true;ps.forEach(function(p){acc=acc.concat(p);if(p.length<PAGE)full=false;});if(full)return wave(start+PAR);});}\n"
    # 필드 단위 병합: 로컬 값이 있는 필드는 유지, 빈 필드(판정/URL/GT후보/메모)만 서버 값으로 보충.
    # 로컬이 완전히 빈 샘플은 자연히 전체가 서버 값으로 채워진다(구버전 동작 포함).
    # 동시에 각 샘플의 서버 시그니처(SERVERSIG)를 기록해 '로컬만 있고 서버엔 없는' 미동기화분을 판별.
    "    function apply(rows){var n=0;rows.forEach(function(x){var id=String(x.sample_id);\n"
    "      if(!sampById[id])return;\n"
    "      var stags=String(x.tags||'').split('|').filter(Boolean),spicks=String(x.gt_candidates||'').split('|').filter(Boolean),surl=String(x.url||''),sbody=String(x.note||'');\n"
    "      SERVERSIG[id]=sigOf(stags,surl,spicks,sbody);\n"
    "      var loc=noteParts(id);\n"
    "      var tags=loc.tags.length?loc.tags:stags;\n"
    "      var picks=(loc.gtPicks&&loc.gtPicks.length)?loc.gtPicks:spicks;\n"
    "      var url=(loc.url&&loc.url.trim())?loc.url:surl;\n"
    "      var body=loc.body.trim()!==''?loc.body:sbody;\n"
    "      var t=noteCombine(tags,url,picks,body);\n"
    "      if(t.trim()!==''&&t!==noteGet(id)&&noteSet(id,t))n++;});\n"
    "      if(n){dirty=false;try{refilter();}catch(e){}try{updateTagSum();}catch(e){}setSyncStat('서버에서 '+n+'건 복원/보강','ok');}\n"
    "      PULLED=true;renderUnsynced();}\n"
    "    wave(0).then(function(){apply(acc);}).catch(function(){setUnsyncErr();});}\n"
    # --- 미동기화 안전장치: 로컬(브라우저)에만 있고 서버에 반영 안 된 검수 감지 + 일괄 저장 ---
    "var SERVERSIG={},PULLED=false;\n"
    # 로컬/서버 시그니처를 동일 정규화(빈값 제거+정렬)로 비교 — 순서/빈태그/구분자 차이 오탐 제거.
    "function sigOf(tags,url,picks,body){return (tags||[]).filter(Boolean).sort().join('|')+'##'+(url||'')+'##'+(picks||[]).filter(Boolean).sort().join('|')+'##'+(body||'');}\n"
    # rjoin: 로컬 배열을 서버 왕복(join '|' -> split '|')과 동일하게 정규화 -> '|' 포함 값도 정합.
    "function rjoin(a){return (a||[]).join('|').split('|');}\n"
    "function locSig(id){var p=noteParts(id);return sigOf(rjoin(p.tags),p.url,rjoin(p.gtPicks),p.body);}\n"
    "function locEmpty(id){var p=noteParts(id);return !((p.tags&&p.tags.length)||(p.gtPicks&&p.gtPicks.length)||(p.url&&p.url.trim())||(p.body&&p.body.trim()));}\n"
    "function unsyncedIds(){var out=[];for(var id in sampById){if(locEmpty(id))continue;if(SERVERSIG[String(id)]!==locSig(id))out.push(id);}return out;}\n"
    "function setUnsyncErr(){var el=document.getElementById('unsyncbar');if(!el)return;el.className='unsync';el.textContent='\\u26a0 \\ub3d9\\uae30\\ud654 \\uc0c1\\ud0dc \\ud655\\uc778 \\uc2e4\\ud328 \\u2014 \\uc0c8\\ub85c\\uace0\\uce68 \\uad8c\\uc7a5';}\n"
    "function renderUnsynced(){var el=document.getElementById('unsyncbar');if(!el)return;\n"
    "      if(!PULLED){el.className='unsync';el.textContent='\\ub3d9\\uae30\\ud654 \\ud655\\uc778 \\uc911\\u2026';return;}\n"
    "      var n=unsyncedIds().length;\n"
    "      if(!n){el.className='unsync ok';el.textContent='\\u2714 \\ubaa8\\ub450 \\uc800\\uc7a5\\ub428';return;}\n"
    "      el.className='unsync warn';el.innerHTML='\\u26a0 \\ubbf8\\uc800\\uc7a5 <b>'+n+'</b>\\uac74 <button id=\"pushall\">\\uc804\\uccb4 \\uc800\\uc7a5</button> <button id=\"showunsync\">\\ubaa9\\ub85d</button>';\n"
    "      var b=document.getElementById('pushall');if(b)b.onclick=pushAllUnsynced;\n"
    "      var b2=document.getElementById('showunsync');if(b2)b2.onclick=showUnsyncList;}\n"
    # 목록: 미저장 샘플을 이름+id로 표시(+콘솔) — '어떤 샘플?' 즉시 확인.
    "function showUnsyncList(){var ids=unsyncedIds();var list=ids.map(function(x){var s=sampById[String(x)];return (s&&s.name?s.name:'(?)')+'  |  '+x;});\n"
    "      console.warn('\\ubbf8\\uc800\\uc7a5 \\ubaa9\\ub85d('+ids.length+'):',list);try{alert('\\ubbf8\\uc800\\uc7a5 '+ids.length+'\\uac74\\n\\n'+list.join('\\n'));}catch(e){}}\n"
    # 전체 저장: 실패분 최대 2회 자동 재시도 + 실패 샘플/서버오류 콘솔 기록.
    "function pushAllUnsynced(){var ids=unsyncedIds();if(!ids.length)return;pushList(ids,0);}\n"
    "function pushList(ids,attempt){var i=0,ok=0,failed=[];\n"
    "      var b=document.getElementById('pushall');if(b){b.disabled=true;b.textContent='\\uc800\\uc7a5 \\uc911\\u2026';}\n"
    "      function step(){if(i>=ids.length){\n"
    "        if(failed.length&&attempt<2){setSyncStat('\\uc7ac\\uc2dc\\ub3c4 '+failed.length+'\\uac74\\u2026','');setTimeout(function(){pushList(failed,attempt+1);},900);return;}\n"
    "        setSyncStat('\\uc800\\uc7a5 \\uc644\\ub8cc '+ok+'\\uac74'+(failed.length?(' / \\uc2e4\\ud328 '+failed.length):''),failed.length?'bad':'ok');\n"
    "        if(failed.length){console.error('\\uc800\\uc7a5 \\uc2e4\\ud328 \\uc0d8\\ud50c:',failed.map(function(x){var s=sampById[String(x)];return {id:x,name:s&&s.name};}));}\n"
    "        renderUnsynced();return;}\n"
    "        var id=ids[i++],rec=vrec(id),s=sampById[String(id)];\n"
    "        fetch(SB_URL+'/rest/v1/reviews?on_conflict=reviewer,sample_id',{method:'POST',headers:{apikey:SB_KEY,Authorization:'Bearer '+SB_KEY,'Content-Type':'application/json',Prefer:'resolution=merge-duplicates,return=minimal'},body:JSON.stringify(rec)})\n"
    "          .then(function(r){if(r.ok){ok++;SERVERSIG[String(id)]=locSig(id);}else{failed.push(id);try{r.text().then(function(t){console.error('\\uc800\\uc7a5 \\uc2e4\\ud328',id,s&&s.name,r.status,t);});}catch(e){}}})\n"
    "          .catch(function(){failed.push(id);})\n"
    "          .then(function(){setSyncStat('\\uc800\\uc7a5 '+i+'/'+ids.length,'ok');step();});}\n"
    "      step();}\n"
    # --- 자동 전체저장: 주기(25s) + 창 숨김/이탈 시 미저장분 자동 반영(유실 방지 강화) ---
    "function autoFlush(urgent){if(!PULLED||!SB_URL||!SB_KEY||!reviewer)return;var ids=unsyncedIds();if(!ids.length)return;\n"
    "  if(urgent){ids.forEach(function(id){var rec=vrec(id);\n"
    # keepalive: 페이지 이탈 중에도 요청 완주(sendBeacon은 커스텀 헤더 불가라 fetch keepalive 사용).
    "    try{fetch(SB_URL+'/rest/v1/reviews?on_conflict=reviewer,sample_id',{method:'POST',keepalive:true,headers:{apikey:SB_KEY,Authorization:'Bearer '+SB_KEY,'Content-Type':'application/json',Prefer:'resolution=merge-duplicates,return=minimal'},body:JSON.stringify(rec)}).then(function(r){if(r.ok)SERVERSIG[String(id)]=locSig(id);}).catch(function(){});}catch(e){}});}\n"
    "  else{pushList(ids,0);}}\n"
    "setInterval(function(){autoFlush(false);},25000);\n"
    "document.addEventListener('visibilitychange',function(){if(document.visibilityState==='hidden')autoFlush(true);});\n"
    "window.addEventListener('pagehide',function(){autoFlush(true);});\n"
    "setTimeout(syncPull,0);"  # 스크립트 초기화(reviewer/sampById/렌더) 완료 후 실행
)
SYNCVAR_OLD = "var SYNC=D.sync_url||'', reviewer='';"
SYNCVAR_NEW = "var SYNC=D.sync_url||'', reviewer='';var SB_URL=D.supabase_url||'',SB_KEY=D.supabase_key||'';"

CATNAV_CSS = (
    ".catnav{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px;"
    "font-size:13px;padding-top:8px;border-top:1px solid var(--line-soft)}"
    ".catnav a{color:var(--accent);text-decoration:none;padding:3px 9px;border:1px solid var(--line);"
    "border-radius:7px}.catnav a:hover{background:var(--surface-2)}"
    ".catnav .cn-cur{font-weight:700;color:var(--ink)}"
    ".catnav .cn-ord{font-family:var(--mono);color:var(--ink-faint)}"
    # 미동기화 배지(우하단 고정) — 저장 유실 방지 안전장치
    "#unsyncbar{position:fixed;right:14px;bottom:14px;z-index:9999;font-size:13px;padding:8px 12px;"
    "border-radius:10px;box-shadow:0 3px 12px rgba(0,0,0,.18);border:1px solid var(--line);"
    "background:var(--surface);color:var(--ink-soft)}"
    "#unsyncbar.ok{color:var(--ok);border-color:var(--ok)}"
    "#unsyncbar.warn{color:#8a6d00;background:#fff8e1;border-color:#e0b000}"
    "@media(prefers-color-scheme:dark){#unsyncbar.warn{background:#3a2f0a;color:#f0d060;border-color:#8a6d00}}"
    "#unsyncbar button{margin-left:8px;cursor:pointer;border:1px solid currentColor;background:transparent;"
    "color:inherit;border-radius:7px;padding:3px 10px;font-weight:700;font-size:12px}"
    "#unsyncbar button:disabled{opacity:.6;cursor:default}"
)

def patch_html(raw_html: str, label: str, ord_text: str, prev_slug, next_slug) -> str:
    """뷰어 template(HTML)에 sync 교체 + 네비바 주입. 데이터는 호출부에서 이미 교체.
    label=현재 카테고리 표시명, ord_text=네비 순번 표기(예: '3/11' 또는 '추가 2/16')."""
    h = raw_html
    # 1) sync JS 교체
    if SYNCVAR_OLD not in h:
        raise RuntimeError("SYNC var 앵커를 못 찾음")
    h = h.replace(SYNCVAR_OLD, SYNCVAR_NEW, 1)
    h, n = SYNCPUSH_RE.subn(lambda m: NEW_SYNCPUSH, h, count=1)
    if n != 1:
        raise RuntimeError("syncPush 앵커를 못 찾음")
    # 2) CSS 주입 (첫 </style> 앞)
    h = h.replace("</style>", CATNAV_CSS + "</style>", 1)
    # 3) 네비바 주입 (chips div 뒤)
    parts = ['<a href="../../index.html">◀ 전체 목록</a>',
             f'<span class="cn-ord">{_html.escape(ord_text)}</span>',
             f'<span class="cn-cur">{_html.escape(label)}</span>']
    if prev_slug:
        parts.append(f'<a href="{prev_slug}.html">◀ 이전</a>')
    if next_slug:
        parts.append(f'<a href="{next_slug}.html">다음 ▶</a>')
    nav = '<div class="catnav">' + "".join(parts) + "</div>"
    anchor = '<div class="chips" id="chips"></div>'
    # 네비바 + 미동기화 배지(우하단 고정) 주입
    unsyncbar = '<div id="unsyncbar" class="unsync"></div>'
    h = h.replace(anchor, anchor + "\n    " + nav + "\n    " + unsyncbar, 1)
    return h

INDEX_TEMPLATE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>레이블링 검수 보드</title>
<style>
:root{--bg:#f5f7fa;--surface:#fff;--surface-2:#eef1f6;--ink:#1a2030;--ink-soft:#586178;--ink-faint:#8a93a8;--line:#e0e5ee;--line-soft:#eaeef4;--accent:#3b6ea5;--ok:#1f8a70;--okbg:#e3f3ec;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#0e131d;--surface:#151b28;--surface-2:#1a2130;--ink:#e6ebf4;--ink-soft:#98a3ba;--ink-faint:#6b7488;--line:#28313f;--line-soft:#202834;--accent:#6ea3d8;--ok:#48c6a2;--okbg:#12312a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;font-size:15px;line-height:1.5}
.wrap{max-width:860px;margin:0 auto;padding:24px 18px 60px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--ink-faint);font-size:13px;margin-bottom:20px}
.guide{display:inline-block;margin-bottom:20px;font-size:13px;color:var(--accent);text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:7px 13px}.guide:hover{background:var(--surface-2)}
.who{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:22px}
.who .lbl{font-size:13px;color:var(--ink-soft);margin-right:4px}
.rv{font-size:14px;padding:7px 16px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--ink-soft);cursor:pointer}
.rv[aria-pressed="true"]{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:700}
h2{font-size:14px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.04em;margin:26px 0 10px}
.card{display:flex;align-items:center;gap:14px;padding:13px 16px;border:1px solid var(--line);border-radius:11px;background:var(--surface);text-decoration:none;color:inherit;margin-bottom:9px}
.card:hover{border-color:var(--accent)}
.card.now{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}
.card.done{opacity:.62}
.ord{font-family:var(--mono);font-size:13px;color:var(--ink-faint);width:26px;flex-shrink:0}
.cmid{flex:1;min-width:0}
.clabel{font-weight:600;font-size:15px}.clabel .nowtag{color:var(--accent);font-size:12px;margin-left:8px}
.cbarwrap{height:7px;background:var(--surface-2);border-radius:4px;margin-top:7px;overflow:hidden}
.cbar{height:100%;background:var(--accent);width:0;transition:width .3s}
.cbar.full{background:var(--ok)}
.cnum{font-family:var(--mono);font-size:13px;color:var(--ink-soft);width:96px;text-align:right;flex-shrink:0}
.cnum b{color:var(--ink)}
.hint{color:var(--ink-faint);font-size:12px}
table.team{border-collapse:collapse;width:100%;font-size:13px;margin-top:4px}
table.team th,table.team td{padding:6px 8px;border-bottom:1px solid var(--line-soft);text-align:right}
table.team th{color:var(--ink-faint);font-weight:600;font-size:11px;text-transform:uppercase}
table.team td.l,table.team th.l{text-align:left;font-family:var(--mono)}
table.team tfoot td{border-top:2px solid var(--line);font-weight:700;color:var(--ink)}
.foot{margin-top:30px;color:var(--ink-faint);font-size:12px;border-top:1px solid var(--line-soft);padding-top:14px}
.empty{color:var(--ink-faint);padding:30px 0;text-align:center}
#mine h2.sec{color:var(--accent);margin:24px 0 10px}
table.team tr.secrow td{background:var(--surface-2);color:var(--accent);font-weight:700;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
</style></head><body><div class="wrap">
<h1>레이블링 검수 보드</h1>
<div class="sub">카테고리를 순서대로 검수하세요. 저장은 자동으로 DB에 반영됩니다. 진행률은 GT후보 선정(없음 확정 포함) 완료 기준.</div>
<a class="guide" href="onboarding.html" target="_blank" rel="noopener">📖 GT 복수정답 검수 온보딩 가이드</a>
<div class="who" id="who"><span class="lbl">검수자</span></div>
<div id="mine"><div class="empty">위에서 본인 이름을 선택하세요.</div></div>
<div id="teamwrap" hidden><h2>카테고리별 진행률</h2><div id="team"></div></div>
<div class="foot" id="foot"></div>
</div>
<script>
var M=__MANIFEST__;
var SB=M.supabase, CATS=M.categories, RVS=M.reviewers;
function norm(s){return String(s||'').replace(/\s+/g,'');}
var NORM2SLUG={};CATS.forEach(function(c){NORM2SLUG[c.norm]=c.slug;});
var curSlug=null;
try{curSlug=localStorage.getItem('labeling_rv')||null;}catch(e){}

var whoEl=document.getElementById('who');
RVS.forEach(function(r){
  var b=document.createElement('button');b.className='rv';b.textContent=r.name;
  b.setAttribute('aria-pressed',r.slug===curSlug?'true':'false');
  b.onclick=function(){curSlug=r.slug;try{localStorage.setItem('labeling_rv',r.slug);}catch(e){}
    Array.prototype.forEach.call(whoEl.querySelectorAll('.rv'),function(x){x.setAttribute('aria-pressed','false');});
    b.setAttribute('aria-pressed','true');render();};
  whoEl.appendChild(b);
});

// done[reviewerSlug][catSlug] = Set(sample_id)
var DONE={};
function bucket(rows){
  // 완료행(gt_candidates 비어있지 않음)만 서버에서 필터되어 옴 — 여기선 grp만 매핑.
  var d={};
  rows.forEach(function(x){
    var slug=NORM2SLUG[norm(x.grp)];if(!slug)return;
    // reviewer name -> slug
    var rv=RVS.filter(function(r){return r.name===x.reviewer;})[0];if(!rv)return;
    (d[rv.slug]=d[rv.slug]||{});(d[rv.slug][slug]=d[rv.slug][slug]||{});
    d[rv.slug][slug][x.sample_id]=1;
  });
  return d;
}
function cnt(rvslug,catslug){var o=DONE[rvslug]&&DONE[rvslug][catslug];return o?Object.keys(o).length:0;}

function render(){renderMine();renderTeam();}
function renderMine(){
  var mine=document.getElementById('mine');
  if(!curSlug){mine.innerHTML='<div class="empty">위에서 본인 이름을 선택하세요.</div>';return;}
  var rv=RVS.filter(function(r){return r.slug===curSlug;})[0];
  var firstIncomplete=null;
  CATS.forEach(function(c){var tot=rv.totals[c.slug]||0;if(firstIncomplete===null&&cnt(curSlug,c.slug)<tot&&tot>0)firstIncomplete=c.slug;});
  mine.innerHTML='';
  var vHead=false;  // '추가 검수' 섹션 헤더는 첫 검증 카드 앞에 1회만
  CATS.forEach(function(c,i){
    var tot=rv.totals[c.slug]||0, done=cnt(curSlug,c.slug);
    if(!tot)return;  // 미배정 카테고리(재분배 하차분·본인 미보유 청크 등)는 본인 메뉴에서 숨김
    if(c.verify&&!vHead){var hh=document.createElement('h2');hh.className='sec';
      hh.textContent=M.verify_section||'추가 검수';mine.appendChild(hh);vHead=true;}
    var pctv=Math.round(done/tot*100), full=(done>=tot);
    var isNow=(c.slug===firstIncomplete);
    var a=document.createElement('a');
    a.className='card'+(isNow?' now':'')+(full?' done':'');
    a.href='data/'+curSlug+'/'+c.slug+'.html';
    a.innerHTML='<div class="ord">'+(i+1)+'</div>'+
      '<div class="cmid"><div class="clabel">'+c.label+(isNow?'<span class="nowtag">▶ 지금</span>':'')+'</div>'+
      '<div class="cbarwrap"><div class="cbar'+(full?' full':'')+'" style="width:'+pctv+'%"></div></div></div>'+
      '<div class="cnum"><b>'+done+'</b> / '+tot+'<br>'+pctv+'%</div>';
    mine.appendChild(a);
  });
}
function renderTeam(){
  var tw=document.getElementById('teamwrap');tw.hidden=false;
  var h='<table class="team"><thead><tr><th class="l">카테고리</th>';
  RVS.forEach(function(r){h+='<th>'+r.name+'</th>';});h+='<th>합계</th></tr></thead><tbody>';
  var vHead=false;  // '추가 검수' 구분 행은 첫 검증 카테고리 앞에 1회만
  CATS.forEach(function(c,i){
    if(c.verify&&!vHead){h+='<tr class="secrow"><td class="l" colspan="'+(RVS.length+2)+'">'+(M.verify_section||'추가 검수')+'</td></tr>';vHead=true;}
    h+='<tr><td class="l">'+(i+1)+'. '+c.label+'</td>';var sd=0,st=0;
    RVS.forEach(function(r){var tot=r.totals[c.slug]||0,done=cnt(r.slug,c.slug);sd+=done;st+=tot;
      h+='<td>'+(tot?done+'/'+tot:'–')+'</td>';});
    h+='<td><b>'+sd+'/'+st+'</b> ('+(st?Math.round(sd/st*100):0)+'%)</td></tr>';
  });
  // 합계 행: 각 검수자 열의 총 완료/전체 + 진행률(맨 아래 고정)
  h+='</tbody><tfoot><tr><td class="l">합계</td>';var gsd=0,gst=0;
  RVS.forEach(function(r){var sd=0,st=0;
    CATS.forEach(function(c){var tot=r.totals[c.slug]||0;if(!tot)return;sd+=Math.min(cnt(r.slug,c.slug),tot);st+=tot;});
    gsd+=sd;gst+=st;
    h+='<td><b>'+sd+'/'+st+'</b><br>'+(st?Math.round(sd/st*100):0)+'%</td>';});
  h+='<td><b>'+gsd+'/'+gst+'</b> ('+(gst?Math.round(gsd/gst*100):0)+'%)</td></tr></tfoot>';
  h+='</table>';document.getElementById('team').innerHTML=h;
}

render();  // 먼저 0%로 그림
// 라이브 진행률 fetch — 완료행(gt_candidates 비어있지 않음)만 서버 필터 + 페이지 병렬 요청(wave).
// 기존: 전체행 1000단위 순차 왕복(느림). 개선: 완료행만(행수↓) + PAR개씩 동시 요청.
(function loadProgress(){
  var acc=[], PAGE=1000, PAR=8;
  var base=SB.url+'/rest/v1/reviews?select=reviewer,sample_id,grp&gt_candidates=neq.&order=row_id.asc&limit='+PAGE+'&offset=';
  function gp(off){return fetch(base+off,{headers:{apikey:SB.key,Authorization:'Bearer '+SB.key}}).then(function(r){return r.ok?r.json():[];});}
  function wave(start){var rq=[],i;for(i=0;i<PAR;i++)rq.push(gp((start+i)*PAGE));
    return Promise.all(rq).then(function(ps){var full=true;ps.forEach(function(p){acc=acc.concat(p);if(p.length<PAGE)full=false;});if(full)return wave(start+PAR);});}
  wave(0).then(function(){DONE=bucket(acc);render();})
    .catch(function(){document.getElementById('foot').textContent='⚠ 진행률 불러오기 실패(오프라인?) — 메뉴는 그대로 사용 가능.';});
})();
</script></body></html>
"""


def plan_redistribution(files):
    """이지나 하차분을 나머지 3인에게 파트너 제약 균형 배분(pre-pass).

    반환: (extra, give_ids)
      - extra[target]  : 대상 보드에 주입할 이지나 sample dict 리스트
      - give_ids       : 이지나 보드에서 제거(이관)할 sample id 집합
    """
    holders = collections.defaultdict(lambda: collections.defaultdict(set))  # catnorm->reviewer->{id}
    ezn = collections.defaultdict(dict)  # catnorm -> {id: sample}
    for f in files:
        m = DATA_RE.search(open(f, encoding="utf-8").read())
        if not m:
            raise RuntimeError(f"data 스크립트 앵커를 못 찾음: {f}")
        D = json.loads(m.group(2))
        rev = D.get("reviewer_default") or os.path.basename(f)[6:-5]
        for s in D.get("samples", []):
            sec = section_label(s, rev)
            if sec is None:
                continue
            k = norm(sec)
            if k in REDIST_NORM:
                sid = str(s.get("id"))
                holders[k][rev].add(sid)
                if rev == REDIST_FROM:
                    ezn[k][sid] = s  # 원본 group 유지 — 대상 보드에서 재분류(신규 판별)
        del D
    extra = {t: [] for t in REDIST_TO}
    give_ids = set()
    for k in REDIST_NORM:
        ids = sorted(ezn[k].keys())
        if k in REDIST_HALF_NORM:
            give = [sid for i, sid in enumerate(ids) if i % 2 == 1]  # 절반 이관(이지나 ceil 유지)
        else:
            give = ids  # 전량 이관
        load = collections.Counter()
        for sid in give:
            cand = [t for t in REDIST_TO if sid not in holders[k][t]]  # 아직 안 가진 2명
            cand.sort(key=lambda t: (load[t], REDIST_TO.index(t)))    # 부하 최소 -> 고정순서
            pick = cand[0]
            load[pick] += 1
            extra[pick].append(ezn[k][sid])
            give_ids.add(sid)
    n = sum(len(v) for v in extra.values())
    print(f"[재분배] 이지나 {len(give_ids)}건 이관 -> " +
          ", ".join(f"{t}+{len(extra[t])}" for t in REDIST_TO) + f" (총 {n})")
    return extra, give_ids


def load_verify_sources():
    """board_source/verify_<검수자>.jsonl -> {reviewer: [sample,...]} (파일 순서 유지).
    id/group(phase) 누락은 빌드 중단 — group 없으면 'None' 팬텀 phase가 생기고,
    id 없으면 진행률/복원 매칭이 깨지므로 fail-loud."""
    out = {}
    for f in sorted(glob.glob(os.path.join(SRC_DIR, "verify_*.jsonl"))):
        rev = os.path.basename(f)[len("verify_"):-len(".jsonl")]
        samples = [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
        for s in samples:
            if not str(s.get("id") or "").strip():
                raise RuntimeError(f"verify 소스 {rev}: id 없는 샘플 존재")
            if not str(s.get("group") or "").strip():
                raise RuntimeError(f"verify 소스 {rev}: group(phase) 없는 샘플 id={s.get('id')}")
        out[rev] = samples
    return out


def _spread_chunks(tuples):
    """다청크 phase(예: 패션렌즈 4청크)의 청크들을 단일청크 카드들 사이에 균등 간격으로 배치.
    입력: [(phase, chunk, nchunks), ...] (phase 우선순위·청크 순). 출력: 재배열된 동일 리스트.
    같은 phase의 큰 청크들이 연속으로 붙지 않도록 전체 길이에 균등 분산(간격=n/청크수, 가운데 정렬)."""
    spread = [t for t in tuples if t[2] > 1]   # nchunks>1 (분산 대상)
    fill = [t for t in tuples if t[2] == 1]    # 단일청크 (사이를 채움)
    if not spread:
        return list(tuples)
    n = len(tuples)
    step = n / len(spread)
    pos_of = {int(i * step + step / 2): spread[i] for i in range(len(spread))}  # 간격>=1 → 위치 유일
    out, fi = [], 0
    for pos in range(n):
        if pos in pos_of:
            out.append(pos_of[pos])
        else:
            out.append(fill[fi]); fi += 1
    return out


def plan_verify(verify_by_rev):
    """검증 샘플을 phase(카테고리)별로 묶어 VERIFY_CHUNK 단위 카드로 분할한다.

    반환: (verify_cats, per_rev)
      verify_cats: 전 검수자 공통 카드 목록(순서 고정)
                   [{slug, label, norm, grp_key, phase, chunk, nchunks}]
      per_rev[reviewer]: {slug: [sample,...]}  — 각 sample에 grp_key 주입됨
    카드 순서=phase 총량(전 검수자 합) 내림차순, 동수는 phase 문자열. slug=cv1..cvN.
    한 phase의 카드 수(nchunks)는 '검수자별 최대 보유수' 기준으로 고정(카드 목록을 전원 공통으로
    유지) — 보유수가 적은 검수자는 뒤쪽 청크 totals=0이라 본인 메뉴에서 자동 숨김."""
    tot = collections.Counter()
    maxcnt = collections.Counter()
    for samples in verify_by_rev.values():
        c = collections.Counter(str(s.get("group")) for s in samples)
        for ph, n in c.items():
            tot[ph] += n
            if n > maxcnt[ph]:
                maxcnt[ph] = n
    phase_order = sorted(tot, key=lambda p: (-tot[p], p))

    # phase 우선순위대로 (phase, chunk, nchunks) 나열 후, 다청크 phase(패션렌즈 4청크 등)의
    # 청크들을 단일청크 카드 사이에 균등 간격으로 흩어 배치(연속 배치 방지). slug는 최종
    # 표시 순서대로 cv1..cvN 부여 — grp_key(집계키)는 phase::chunk라 slug 순서와 무관해 안전.
    tuples = []
    for ph in phase_order:
        nch = max(1, math.ceil(maxcnt[ph] / VERIFY_CHUNK))
        for ci in range(1, nch + 1):
            tuples.append((ph, ci, nch))
    ordered = _spread_chunks(tuples)

    verify_cats = []
    slug_of = {}  # (phase, chunk) -> slug
    for vi, (ph, ci, nch) in enumerate(ordered, 1):
        slug = f"cv{vi}"
        label = ph + (f" ({ci}/{nch})" if nch > 1 else "")  # 순수 카테고리명(+분할 표기)
        gk = _vgrpkey(ph, ci)
        slug_of[(ph, ci)] = slug
        verify_cats.append({"slug": slug, "label": label, "norm": norm(gk),
                            "grp_key": gk, "phase": ph, "chunk": ci, "nchunks": nch})

    per_rev = {}
    for rev, samples in verify_by_rev.items():
        by_phase = collections.OrderedDict((p, []) for p in phase_order)
        for s in samples:
            by_phase.setdefault(str(s.get("group")), []).append(s)
        buckets = collections.defaultdict(list)
        for ph in phase_order:
            for i, s in enumerate(by_phase.get(ph, [])):
                ci = i // VERIFY_CHUNK + 1
                s["grp_key"] = _vgrpkey(ph, ci)  # 진행률 집계키(기존 카테고리명과 분리)
                buckets[slug_of[(ph, ci)]].append(s)
        per_rev[rev] = dict(buckets)
    n = sum(len(v) for ss in per_rev.values() for v in ss.values())
    print(f"[추가검수] {len(verify_cats)}개 카드(phase {len(phase_order)}종, chunk {VERIFY_CHUNK}) / 총 {n}건")
    return verify_cats, per_rev


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    # 구 슬러그(c7·구 data/verify 등) 잔존 방지: 카테고리 데이터는 매 빌드마다 새로 생성.
    # 추가 검수 카드도 이제 data/<rN>/cvX.html로 통합되므로 별도 보존 예외 없음.
    for d in glob.glob(os.path.join(OUT_DIR, "data", "*")):
        if os.path.islink(d) or os.path.isfile(d):
            os.remove(d)
        elif os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    reviewers = []  # {name, slug, totals:{catnorm:count}}
    files = glob.glob(os.path.join(SRC_DIR, "board_*.html"))
    if not files:
        raise SystemExit(f"입력 board_*.html 없음: {SRC_DIR}")
    # rN 슬러그를 REVIEWER_ORDER로 고정(배포 URL 안정성). reviewer_default만 가볍게 추출해 정렬.
    def _rev_of(p):
        m = re.search(r'"reviewer_default"\s*:\s*"([^"]*)"', open(p, encoding="utf-8").read())
        return m.group(1) if m else os.path.basename(p)[6:-5]
    _rev = {p: _rev_of(p) for p in files}
    files.sort(key=lambda p: (REVIEWER_ORDER.index(_rev[p]) if _rev[p] in REVIEWER_ORDER else 999, p))

    # 재분배 계산(pre-pass): 이지나 하차분을 나머지 3인 보드로 이관.
    extra, give_ids = plan_redistribution(files)
    missing_img = 0  # IMG_MAP에 없어 이미지 URL이 안 붙은 렌더 샘플 수(경고용)

    # 추가 검수 사전배치(pre-pass): 검증 샘플을 phase별 chunk 카드로 분할(전원 공통 카드 목록).
    verify_cats, verify_per_rev = plan_verify(load_verify_sources())

    for ri, f in enumerate(files):
        raw = open(f, encoding="utf-8").read()
        m = DATA_RE.search(raw)
        if not m:
            raise RuntimeError(f"data 스크립트 앵커를 못 찾음: {f}")
        D = json.loads(m.group(2))
        reviewer = D.get("reviewer_default") or os.path.basename(f)[6:-5]
        rslug = f"r{ri+1}"
        template = raw  # 데이터는 카테고리별로 갈아끼움

        # 재분배 적용: 대상 보드엔 이지나 이관분 주입, 이지나 보드에선 이관분 제거.
        samples = list(D.get("samples", []))
        if reviewer in REDIST_TO:
            samples += extra.get(reviewer, [])

        # 섹션별 샘플 분할 (제외 스킵 + 기존카테고리 신규분은 '… 신규' 섹션으로)
        by_cat = collections.defaultdict(list)
        for s in samples:
            if reviewer == REDIST_FROM and str(s.get("id")) in give_ids:
                continue  # 이관된 샘플은 이지나 보드에서 제외
            sec = section_label(s, reviewer)
            if sec is None:
                continue
            s["group"] = sec  # syncPush가 s.group을 grp로 저장 -> 진행률 섹션 정합
            sid = str(s.get("id"))
            if sid in IMG_MAP:
                # base64 -> 공개 S3 URL (뷰어는 _img_data 값을 <img src>에 그대로 사용)
                s["_img_data"] = S3_BASE + IMG_MAP[sid]
            k = norm(sec)
            if k in CAT_SLUG:
                if sid not in IMG_MAP:
                    missing_img += 1  # 슬림 소스엔 base64 없음 -> URL 미주입 시 이미지 깨짐
                by_cat[k].append(s)

        rdir = os.path.join(OUT_DIR, "data", rslug)
        os.makedirs(rdir, exist_ok=True)
        totals = {}
        present = [k for k in NORM_ORDER if by_cat.get(k)]
        for oi, k in enumerate(NORM_ORDER):
            subs = by_cat.get(k, [])
            totals[k] = len(subs)
            if not subs:
                continue
            # 이 카테고리용 payload (runs 등 전역 필드는 유지, samples만 교체)
            Dc = dict(D)
            Dc["samples"] = subs
            Dc["n_samples"] = len(subs)
            Dc["embedded_samples"] = len(subs)
            Dc["sync_url"] = SUPABASE_URL          # SYNC truthy -> 검수자 입력 UI 표시
            Dc["supabase_url"] = SUPABASE_URL
            Dc["supabase_key"] = SUPABASE_KEY
            data_json = json.dumps(Dc, ensure_ascii=False).replace("<", "\\u003c")
            # prev/next: 존재하는 카테고리들 사이에서
            idx_in_present = present.index(k)
            prev_slug = CAT_SLUG[present[idx_in_present-1]] if idx_in_present > 0 else None
            next_slug = CAT_SLUG[present[idx_in_present+1]] if idx_in_present < len(present)-1 else None
            html_out = DATA_RE.sub(lambda mm: mm.group(1) + data_json + mm.group(3), template, count=1)
            html_out = patch_html(html_out, CAT_LABEL[k], f"{oi+1}/{len(NORM_ORDER)}", prev_slug, next_slug)
            outp = os.path.join(rdir, CAT_SLUG[k] + ".html")
            open(outp, "w", encoding="utf-8").write(html_out)
        # --- 추가 검수 카드 방출: 기존 c1~c11과 동일 파이프라인(runs만 검증용으로 교체) ---
        v_totals = {}
        v_present = [vc["slug"] for vc in verify_cats
                     if verify_per_rev.get(reviewer, {}).get(vc["slug"])]
        for vc in verify_cats:
            subs = verify_per_rev.get(reviewer, {}).get(vc["slug"], [])
            v_totals[vc["slug"]] = len(subs)
            if not subs:
                continue
            Dv = dict(D)                           # 전역 필드 유지, samples/runs만 검증용으로 교체
            Dv["runs"] = VERIFY_RUNS
            Dv["n_runs"] = 1
            Dv["samples"] = subs
            Dv["n_samples"] = len(subs)
            Dv["embedded_samples"] = len(subs)
            Dv["sync_url"] = SUPABASE_URL
            Dv["supabase_url"] = SUPABASE_URL
            Dv["supabase_key"] = SUPABASE_KEY
            data_json = json.dumps(Dv, ensure_ascii=False).replace("<", "\\u003c")
            pi = v_present.index(vc["slug"])
            prev_slug = v_present[pi - 1] if pi > 0 else None
            next_slug = v_present[pi + 1] if pi < len(v_present) - 1 else None
            html_out = DATA_RE.sub(lambda mm: mm.group(1) + data_json + mm.group(3), template, count=1)
            html_out = patch_html(html_out, vc["label"], f"추가 {pi+1}/{len(v_present)}", prev_slug, next_slug)
            open(os.path.join(rdir, vc["slug"] + ".html"), "w", encoding="utf-8").write(html_out)

        # 데이터 무결성: 같은 (검수자, sample_id)가 기존 카드와 추가 검수 카드에 동시에 있으면
        # Supabase 단일 행(on_conflict=reviewer,sample_id)을 두 보드가 다투게 됨(집계 충돌) → 빌드 중단.
        board_ids = {str(s.get("id")) for subs in by_cat.values() for s in subs}
        v_ids = {str(s.get("id")) for subs in verify_per_rev.get(reviewer, {}).values() for s in subs}
        clash = board_ids & v_ids
        if clash:
            raise RuntimeError(f"{reviewer}: 기존 보드와 추가 검수에 동시 존재하는 sample_id "
                               f"{len(clash)}건(집계 행 충돌) — 예: {sorted(clash)[:3]}")

        reviewers.append({"name": reviewer, "slug": rslug, "totals": totals, "vtotals": v_totals})
        print(f"[{reviewer}] {rslug}: " + ", ".join(f"{CAT_SLUG[k]}={totals[k]}" for k in NORM_ORDER)
              + f" | 추가검수 {sum(v_totals.values())}건({len(v_present)}카드)")

    if missing_img:
        print(f"⚠ 경고: 이미지 URL 미주입 {missing_img}건 — image_urls.json 커버리지 확인 필요"
              f"(슬림 소스엔 base64 없음 → 해당 샘플 이미지 깨짐)")

    # manifest + index — 기존 카테고리(verify:false) + 추가 검수 카드(verify:true)를 한 목록으로.
    categories = [{"slug": CAT_SLUG[norm(c)], "label": c, "norm": norm(c), "verify": False}
                  for c in TARGET_ORDER]
    categories += [{"slug": vc["slug"], "label": vc["label"], "norm": vc["norm"], "verify": True}
                   for vc in verify_cats]

    def _rv_totals(r):
        t = {CAT_SLUG[k]: r["totals"].get(k, 0) for k in NORM_ORDER}
        t.update({vc["slug"]: r.get("vtotals", {}).get(vc["slug"], 0) for vc in verify_cats})
        return t

    manifest = {
        "supabase": {"url": SUPABASE_URL, "key": SUPABASE_KEY},
        "categories": categories,
        "reviewers": [{"name": r["name"], "slug": r["slug"], "totals": _rv_totals(r)} for r in reviewers],
        "verify_section": VERIFY_SECTION,
    }
    open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8").write(
        json.dumps(manifest, ensure_ascii=False, indent=2))
    idx = INDEX_TEMPLATE.replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False))
    open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8").write(idx)
    print("manifest + index written. reviewers:", [r["name"] for r in reviewers])

if __name__ == "__main__":
    main()

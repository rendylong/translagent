# -*- coding: utf-8 -*-
"""live 会话转正与断电恢复(Task 9)。

finalize_session(session_dir, speakers_hint, force_tail, jobs_dir, queue) -> job_id
  把一个 live 会话目录转正成普通 job,走与 create_job 完全同构的 meta:
    ① live_meta 状态置 stopping(幂等;live 停止路径早已置过,崩溃恢复路径在此落);
    ② force_tail 可调用对象非空时调用之——补跑尾窗,由 LiveSession 的 Pass2 驱动线程
      提供闭包;崩溃恢复路径没有驱动线程,不补跑(音频保全即转正,spec §6.7);
    ③ 回填 WAV 头(RIFF/data 长度按文件实际大小;对已 finalize 的文件是幂等重写,
      对崩溃残留的 0 长度头文件是唯一修复手段);
    ④ 新建 jobs/<new>/,硬链接 live_audio.wav → source.wav(同卷零拷贝;跨卷回落复制);
    ⑤ 写 meta(status='queued',language/num_speakers/title 透传,title 前缀「实时 」,
      已带前缀不重复加)入 job_queue;
    ⑥ live_meta 置 finalized 后删 live 目录(硬链接下不占双空间;转正中途崩溃留下的
      finalized 残目录由 recover_all 清)。
  返回 job_id。全程同步,由调用方决定在哪个线程跑(live 停止路径在转正线程)。

recover_all(live_root) -> list[(session_id, job_id)]
  启动扫描(app.live_recover 真身):live/ 下含 live_meta.json 的目录——
  非 finalized → 转正(无尾窗补跑);finalized 残留 → 只删目录;
  无 live_audio.wav 或 ≤44 字节的空壳 → 无可救音频,直接删。单个目录失败不阻断其余。
"""
import json
import os
import shutil
import struct
import sys
import time
import uuid

META = 'live_meta.json'
AUDIO = 'live_audio.wav'
TERMINAL_LIVE = 'finalized'
_LANGS = ('zh', 'en', 'ja', 'auto')


def _read_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def patch_wav_header(path):
    """按文件实际大小回填 RIFF(偏移 4)与 data 长度,返回 data 字节数。幂等。

    崩溃恢复场景 AppendWav 未来得及 finalize,头部两处长度仍是 0;live 正常停止
    路径已由 AppendWav.finalize 回填,此处重写同样数值。data 块用 RIFF chunk 遍历
    定位(AppendWav 产物恒为 44 字节头,遍历是防御)。"""
    if os.path.getsize(path) < 44:
        raise ValueError('WAV 文件不足 44 字节头:%s' % path)
    with open(path, 'r+b') as f:
        if f.read(12)[:4] != b'RIFF':
            raise ValueError('不是 WAV 文件:%s' % path)
        pos = 12
        while True:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = struct.unpack('<4sI', hdr)
            if cid == b'data':
                data_len = os.path.getsize(path) - (pos + 8)
                data_len -= data_len % 2
                f.seek(4)
                f.write(struct.pack('<I', os.path.getsize(path) - 8))
                f.seek(pos + 4)
                f.write(struct.pack('<I', data_len))
                return data_len
            pos += 8 + size + (size % 2)
    raise ValueError('WAV 中未找到 data 块:%s' % path)


def finalize_session(session_dir, speakers_hint=None, force_tail=None,
                     jobs_dir=None, job_queue=None):
    """live 目录 → 普通 job(六步见模块 docstring)。返回 job_id。"""
    import app   # 懒导入:app 顶层 import live_session→live_finalize 时零环
    jobs_dir = jobs_dir if jobs_dir is not None else app.JOBS_DIR
    job_queue = job_queue if job_queue is not None else app.job_queue

    meta = _read_json(os.path.join(session_dir, META))
    meta['status'] = 'stopping'
    _write_json(os.path.join(session_dir, META), meta)

    if force_tail is not None:
        force_tail()

    wav = os.path.join(session_dir, AUDIO)
    patch_wav_header(wav)

    job_id = time.strftime('%Y%m%d-%H%M%S') + '_' + uuid.uuid4().hex[:4]
    jd = os.path.join(jobs_dir, job_id)
    os.makedirs(jd, exist_ok=True)
    src = os.path.join(jd, 'source.wav')
    try:
        os.link(wav, src)                     # 同卷硬链接,零拷贝
    except OSError:                           # 跨卷(测试 tmp 等)回落复制
        shutil.copyfile(wav, src)

    language = meta.get('language')
    if language not in _LANGS:
        language = 'zh'
    try:
        nspeakers = int(speakers_hint if speakers_hint is not None
                        else meta.get('speakers') or 0)
        assert -1 <= nspeakers <= 10
    except (TypeError, ValueError, AssertionError):
        nspeakers = 0
    title = str(meta.get('title') or '实时会议').strip()[:80] or '实时会议'
    if not title.startswith('实时'):
        title = '实时 ' + title
    jmeta = {'id': job_id, 'title': title, 'created_at': time.strftime('%Y-%m-%d %H:%M'),
             'language': language, 'num_speakers': nspeakers, 'source_file': 'source.wav',
             'status': 'queued', 'stage': '排队中', 'progress': 0, 'error': None,
             'duration_s': None, 'num_speakers_detected': None, 'speaker_names': {},
             'voiceprint': True, 'speaker_map': {}, 'live_session': meta.get('id')}
    _write_json(os.path.join(jd, 'meta.json'), jmeta)
    job_queue.put(job_id)

    meta['status'] = TERMINAL_LIVE            # 转正成功后才置终态;此后的崩溃由 recover 清残目录
    _write_json(os.path.join(session_dir, META), meta)
    shutil.rmtree(session_dir, ignore_errors=True)
    return job_id


def recover_all(live_root):
    """启动扫描 live/:非终态会话全部转正(无尾窗补跑),终态残目录与空壳清理。"""
    out = []
    if not os.path.isdir(live_root):
        return out
    for name in sorted(os.listdir(live_root)):
        d = os.path.join(live_root, name)
        if not os.path.isdir(d) or name.startswith('_'):
            continue
        mp = os.path.join(d, META)
        if not os.path.isfile(mp):
            continue
        try:
            meta = _read_json(mp)
        except (json.JSONDecodeError, OSError) as e:
            print('live 恢复:跳过坏 meta %s(%s)' % (name, e), file=sys.stderr)
            continue
        if meta.get('status') == TERMINAL_LIVE:
            shutil.rmtree(d, ignore_errors=True)   # 转正中途崩溃的残目录:job 已建成
            continue
        wav = os.path.join(d, AUDIO)
        if not os.path.isfile(wav) or os.path.getsize(wav) <= 44:
            shutil.rmtree(d, ignore_errors=True)   # 无可救音频的空壳
            continue
        try:
            jid = finalize_session(d, speakers_hint=meta.get('speakers'))
            out.append((name, jid))
        except Exception as e:  # noqa: BLE001 单目录失败不阻断其余
            print('live 恢复失败 %s:%s' % (name, e), file=sys.stderr)
    return out

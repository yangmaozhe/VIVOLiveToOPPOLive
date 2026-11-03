#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vivo -> OPPO 实况照片批量转换

vivo(OriginOS) 的实况照片是「IMG_xxx.jpg + IMG_xxx.mp4」两个文件；
OPPO/一加(ColorOS 15+) 的实况照片是「单个 jpg」，mp4 直接追加在 JPEG 末尾，
由 XMP(GCamera/OpCamera/Container) 与 moov 里的 lpex 盒子描述。

本脚本把同一目录下同名的 jpg+mp4 合成一个 OPPO 可识别的单文件实况照片，
只重新封装、不重新编码视频，画质无损。

用法:
    python vivo2oppo_live.py                     # 转换 ./vivo_live -> ./oppo_ready
    python vivo2oppo_live.py <输入目录> -o <输出目录>
    python vivo2oppo_live.py <输入目录> --no-lpex  # 只写 XMP，不写 lpex（排障用）
    python vivo2oppo_live.py <总目录> -o <输出目录> --mirror
        # 递归处理所有子文件夹，保持原有目录结构：
        # 有同名 mp4 的 jpg 转成 OPPO 单文件实况图（原 jpg+mp4 不再输出），
        # 其余 jpg/png/heic 等文件原样复制。
    python vivo2oppo_live.py <总目录> -o <输出目录> --mirror --heic-to-jpeg [all]
        # 额外把 HEIC 转成 JPEG（重新编码一次）：
        # 不带参数=只转带配对 mp4 的 HEIC 并做成实况；all=所有 HEIC 都转
        # 需要先安装: python -m pip install pillow-heif
    # 中断后续跑：加 --resume，已完整生成的文件会自动跳过
"""

import argparse
import json
import os
import re
import shutil
import struct
import sys
import tempfile
from pathlib import Path

XMP_HEADER = b'http://ns.adobe.com/xap/1.0/\x00'

CONTAINERS = {
    'moov', 'trak', 'mdia', 'minf', 'stbl', 'dinf', 'edts', 'mvex', 'udta',
    'tref', 'ipro', 'sinf', 'schi', 'meco', 'hnti', 'hinf',
}


# ---------------------------------------------------------------- JPEG 部分

def jpeg_end(data):
    """返回主图 JPEG 的结束位置（EOI 之后），遇到非法结构时报错。"""
    if data[:2] != b'\xff\xd8':
        raise ValueError('不是 JPEG 文件')
    p = 2
    n = len(data)
    while p + 1 < n:
        if data[p] != 0xFF:
            raise ValueError(f'JPEG 结构异常 @{p}')
        m = data[p + 1]
        if m == 0xD9:
            return p + 2
        if m == 0x01 or 0xD0 <= m <= 0xD7:
            p += 2
            continue
        if m == 0xDA:  # SOS，之后是熵编码数据
            seglen = struct.unpack('>H', data[p + 2:p + 4])[0]
            q = p + 2 + seglen
            while q + 1 < n:
                if data[q] == 0xFF:
                    nxt = data[q + 1]
                    if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                        q += 2
                        continue
                    if nxt == 0xFF:
                        q += 1
                        continue
                    break
                q += 1
            p = q
            continue
        seglen = struct.unpack('>H', data[p + 2:p + 4])[0]
        p += 2 + seglen
    raise ValueError('JPEG 缺少 EOI')


def insert_app1(jpeg, payload):
    """把一段 APP1 数据插到 EXIF(APP1) 之后；没有 EXIF 就紧跟 SOI。"""
    p = 2
    while p + 4 <= len(jpeg):
        if jpeg[p] != 0xFF:
            break
        m = jpeg[p + 1]
        if m in (0xD8, 0xD9, 0x01) or 0xD0 <= m <= 0xD7:
            p += 2
            continue
        seglen = struct.unpack('>H', jpeg[p + 2:p + 4])[0]
        is_exif = m == 0xE1 and jpeg[p + 4:p + 10] == b'Exif\x00\x00'
        if is_exif:
            p += 2 + seglen
            break
        p += 2 + seglen
    else:
        p = 2
    seg = b'\xff\xe1' + struct.pack('>H', len(payload) + 2) + payload
    if len(payload) + 2 > 0xFFFF:
        raise ValueError('XMP 过大，无法放进单个 APP1 段')
    return jpeg[:p] + seg + jpeg[p:]


# ---------------------------------------------------------------- MP4 部分

def iter_boxes(buf, start, end):
    p = start
    while p + 8 <= end:
        size = struct.unpack('>I', buf[p:p + 4])[0]
        typ = buf[p + 4:p + 8].decode('latin1', 'replace')
        hdr = 8
        if size == 1:
            size = struct.unpack('>Q', buf[p + 8:p + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - p
        if size < hdr or p + size > end:
            raise ValueError(f'mp4 盒子异常: {typ} @{p} size={size}')
        yield typ, p, size, hdr
        p += size


def walk(buf, start, end, out):
    for typ, p, size, hdr in iter_boxes(buf, start, end):
        out.append((typ, p, size, hdr))
        if typ in CONTAINERS:
            walk(buf, p + hdr, p + size, out)
        elif typ == 'meta':
            # 标准 mp4 的 meta 带 4 字节 version/flags，QuickTime 风格没有
            body = p + hdr
            if buf[body + 4:body + 8] != b'hdlr':
                body += 4
            walk(buf, body, p + size, out)
        elif typ == 'stsd':
            walk(buf, p + hdr + 8, p + size, out)


def parse_mp4(buf):
    info = {'boxes': [], 'moov': None, 'timescale': 0, 'duration': 0,
            'video': None, 'stco': [], 'co64': []}
    for typ, p, size, hdr in iter_boxes(buf, 0, len(buf)):
        if typ == 'moov':
            info['moov'] = (p, size, hdr)
    if not info['moov']:
        raise ValueError('mp4 中没有 moov')

    boxes = []
    walk(buf, 0, len(buf), boxes)
    info['boxes'] = boxes

    for typ, p, size, hdr in boxes:
        body = p + hdr
        if typ == 'mvhd':
            v = buf[body:body + size - hdr]
            if v[0] == 0:
                ts, dur = struct.unpack('>II', v[12:20])
            else:
                ts, dur = struct.unpack('>IQ', v[20:32])
            info['timescale'], info['duration'] = ts, dur
        elif typ == 'stco':
            n = struct.unpack('>I', buf[body + 4:body + 8])[0]
            info['stco'].append((body + 8, n))
        elif typ == 'co64':
            n = struct.unpack('>I', buf[body + 4:body + 8])[0]
            info['co64'].append((body + 8, n))

    # 找到 vide 轨道对应的 tkhd / mdhd
    moov_p, moov_size, moov_hdr = info['moov']
    for typ, p, size, hdr in boxes:
        if typ != 'trak' or not (moov_p <= p < moov_p + moov_size):
            continue
        kids = [(t, pp, s, h) for (t, pp, s, h) in boxes if p <= pp < p + size]
        hdlr = [x for x in kids if x[0] == 'hdlr']
        if not hdlr:
            continue
        hb = hdlr[0][1] + hdlr[0][3]
        handler = buf[hb + 8:hb + 12]
        if handler != b'vide':
            continue
        tkhd = [x for x in kids if x[0] == 'tkhd'][0]
        tb = tkhd[1] + tkhd[3]
        v = buf[tb:tb + tkhd[2] - tkhd[3]]
        matrix = struct.unpack('>9i', v[40:76])
        w, h = struct.unpack('>II', v[76:84])
        mdhd = [x for x in kids if x[0] == 'mdhd'][0]
        mb = mdhd[1] + mdhd[3]
        mv = buf[mb:mb + mdhd[2] - mdhd[3]]
        if mv[0] == 0:
            ts, dur = struct.unpack('>II', mv[12:20])
        else:
            ts, dur = struct.unpack('>IQ', mv[20:32])
        info['video'] = {
            'width': w / 65536.0, 'height': h / 65536.0,
            'matrix': matrix, 'timescale': ts, 'duration': dur,
            'orientation': matrix_orientation(matrix),
        }
    return info


def matrix_orientation(m):
    a, b, _, c, d = m[0], m[1], m[2], m[3], m[4]
    if a == 0 and b > 0 and c < 0 and d == 0:
        return 90
    if a < 0 and b == 0 and c == 0 and d < 0:
        return 180
    if a == 0 and b < 0 and c > 0 and d == 0:
        return 270
    return 0


def build_lpex(cover_us, video, duration_us):
    payload = {
        'coverFramePts': cover_us,
        'desc': 'OppoMotionVideoExt',
        'frameInterpolationInfo': [cover_us, cover_us],
        'matrixCount': 0,
        'photoCropFactor': 0.0,
        'photoCropRect': [0, 0, 0, 0],
        'photoEisCropFactor': [1.0, 1.0],
        'photoEisMatrix': [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        'subVideoScaleFactor': 1.0,
        'version': 1,
        'videoOrientation': video['orientation'],
        'videoSize': [int(round(video['width'])), int(round(video['height']))],
    }
    js = json.dumps(payload, separators=(',', ':')).encode('ascii')
    body = b'LivePhotoExtension' + js
    return struct.pack('>I', len(body) + 8) + b'lpex' + body


def add_lpex(mp4, lpex):
    """把 lpex 盒子追加到 moov 末尾，并修正 moov 尺寸与 chunk 偏移。"""
    info = parse_mp4(mp4)
    moov_p, moov_size, moov_hdr = info['moov']
    insert_pos = moov_p + moov_size
    delta = len(lpex)

    out = bytearray(mp4)
    # 修正 stco / co64 中位于插入点之后的绝对偏移
    for off, n in info['stco']:
        for i in range(n):
            q = off + 4 * i
            val = struct.unpack('>I', out[q:q + 4])[0]
            if val >= insert_pos:
                struct.pack_into('>I', out, q, val + delta)
    for off, n in info['co64']:
        for i in range(n):
            q = off + 8 * i
            val = struct.unpack('>Q', out[q:q + 8])[0]
            if val >= insert_pos:
                struct.pack_into('>Q', out, q, val + delta)
    # 修正 moov 尺寸
    if moov_hdr == 8:
        struct.pack_into('>I', out, moov_p, moov_size + delta)
    else:
        struct.pack_into('>Q', out, moov_p + 8, moov_size + delta)
    out[insert_pos:insert_pos] = lpex
    return bytes(out)


# ---------------------------------------------------------------- XMP

def build_xmp(cover_us, video_len):
    return (b'<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.6-c140 79.160451, 2017/05/06-01:08:21        ">\n'
            b'   <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            b'      <rdf:Description rdf:about=""\n'
            b'            xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"\n'
            b'            xmlns:OpCamera="http://ns.oplus.com/photos/1.0/camera/"\n'
            b'            xmlns:Container="http://ns.google.com/photos/1.0/container/"\n'
            b'            xmlns:Item="http://ns.google.com/photos/1.0/container/item/"\n'
            b'         GCamera:MotionPhoto="1"\n'
            b'         GCamera:MotionPhotoVersion="1"\n'
            b'         GCamera:MotionPhotoPresentationTimestampUs="' + str(cover_us).encode() + b'"\n'
            b'         OpCamera:MotionPhotoPrimaryPresentationTimestampUs="' + str(cover_us).encode() + b'"\n'
            b'         OpCamera:MotionPhotoOwner="oplus"\n'
            b'         OpCamera:OLivePhotoVersion="2"\n'
            b'         OpCamera:VideoLength="' + str(video_len).encode() + b'">\n'
            b'         <Container:Directory>\n'
            b'            <rdf:Seq>\n'
            b'               <rdf:li rdf:parseType="Resource">\n'
            b'                  <Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary" Item:Length="0" Item:Padding="0"/>\n'
            b'               </rdf:li>\n'
            b'               <rdf:li rdf:parseType="Resource">\n'
            b'                  <Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" Item:Length="' + str(video_len).encode() + b'"/>\n'
            b'               </rdf:li>\n'
            b'            </rdf:Seq>\n'
            b'         </Container:Directory>\n'
            b'      </rdf:Description>\n'
            b'   </rdf:RDF>\n'
            b'</x:xmpmeta>'), XMP_HEADER


def build_xmp_segment(cover_us, video_len):
    packet, header = build_xmp(cover_us, video_len)
    return header + packet


# ---------------------------------------------------------------- 校验

def verify(out_path, jpeg_len, video_len):
    data = out_path.read_bytes()
    problems = []
    total = jpeg_len + video_len
    if len(data) != total:
        problems.append(f'文件长度异常 {len(data)} != {total}')
    m = data.find(b'GCamera:MotionPhoto="1"')
    if m < 0:
        problems.append('缺少 XMP MotionPhoto 标记')
    if f'OpCamera:VideoLength="{video_len}"'.encode() not in data:
        problems.append('XMP 视频长度与实际不一致')
    v = data[jpeg_len:]
    if v[4:8] != b'ftyp':
        problems.append('JPEG 之后不是 mp4 数据')
    try:
        boxes = list(iter_boxes(v, 0, len(v)))
        if sum(b[2] for b in boxes) != len(v):
            problems.append('mp4 盒子总长与视频长度不一致')
        info = parse_mp4(v)
        if info['video'] is None:
            problems.append('mp4 中没有视频轨道')
        bad = 0
        for off, n in info['stco']:
            for i in range(n):
                if struct.unpack('>I', v[off + 4 * i:off + 4 * i + 4])[0] >= len(v):
                    bad += 1
        for off, n in info['co64']:
            for i in range(n):
                if struct.unpack('>Q', v[off + 8 * i:off + 8 * i + 8])[0] >= len(v):
                    bad += 1
        if bad:
            problems.append(f'{bad} 个 chunk 偏移越界')
    except Exception as e:  # noqa: BLE001
        problems.append(f'mp4 解析失败: {e}')
    try:
        from PIL import Image
        with Image.open(out_path) as im:
            im.load()
    except Exception as e:  # noqa: BLE001
        problems.append(f'JPEG 打不开: {e}')
    return problems


# ---------------------------------------------------------------- 主流程

class AlreadyLive(RuntimeError):
    """原图已经自带实况信息，无需转换。"""


def is_oppo_live(path):
    """判断 jpg 是否已经带有实况照片标记（防止重复叠加视频）。"""
    with open(path, 'rb') as f:
        head = f.read(1024 * 1024)
    return b'GCamera:MotionPhoto="1"' in head


def convert(jpg: Path, mp4: Path, out_path: Path, with_lpex=True, cover_ratio=0.4):
    if is_oppo_live(jpg):
        raise AlreadyLive('原图已经含有实况照片标记，跳过')
    jpg_data = jpg.read_bytes()

    jpeg_len = jpeg_end(jpg_data)
    jpeg = jpg_data[:jpeg_len]

    mp4_data = mp4.read_bytes()
    info = parse_mp4(mp4_data)
    if info['video'] is None:
        raise RuntimeError('mp4 中没有视频轨道')
    duration_us = (info['duration'] / info['timescale'] *
                   1_000_000) if info['timescale'] else 0
    cover_us = int(round(duration_us * cover_ratio))

    video = mp4_data
    if with_lpex:
        video = add_lpex(mp4_data, build_lpex(cover_us, info['video'], duration_us))

    video_len = len(video)
    xmp_seg = build_xmp_segment(cover_us, video_len)
    jpeg = insert_app1(jpeg, xmp_seg)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + '.part')
    tmp.write_bytes(jpeg + video)

    problems = verify(tmp, len(jpeg), video_len)
    if problems:
        tmp.unlink(missing_ok=True)
        raise RuntimeError('; '.join(problems))
    os.replace(tmp, out_path)  # 写完再改名，中途中断不会留下半成品
    os.utime(out_path, (jpg.stat().st_atime, jpg.stat().st_mtime))
    return len(jpeg), video_len


def copy_file(src_file: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + '.part')
    shutil.copy2(src_file, tmp)
    os.replace(tmp, dst)


HEIC_EXTS = ('.heic', '.heif')
_heif_ready = False

# EXIF 方向 -> Pillow 旋转方式
_ORIENT_METHOD = {
    2: 'FLIP_LEFT_RIGHT', 3: 'ROTATE_180', 4: 'FLIP_TOP_BOTTOM',
    5: 'TRANSPOSE', 6: 'ROTATE_270', 7: 'TRANSVERSE', 8: 'ROTATE_90',
}


def heic_to_jpeg(src_file: Path, dst: Path):
    """把 HEIC/HEIF 重新编码为 JPEG（保留 EXIF 与 ICC，并摆正像素方向）。"""
    global _heif_ready
    if not _heif_ready:
        try:
            import pillow_heif
        except ImportError:
            raise RuntimeError(
                '转换 HEIC 需要 pillow-heif，请先执行: python -m pip install pillow-heif')
        pillow_heif.register_heif_opener()
        _heif_ready = True
    from PIL import Image
    with Image.open(src_file) as im:
        # pillow-heif 打开时会把 EXIF 方向重置为 1，
        # 原方向放在 info['original_orientation']，需要自己把像素转正。
        ori = im.info.get('original_orientation') or im.getexif().get(0x0112) or 1
        exif = im.info.get('exif')
        icc = im.info.get('icc_profile')
        img = im.convert('RGB')
        method = _ORIENT_METHOD.get(ori)
        if method:
            img = img.transpose(getattr(Image.Transpose, method))
        dst.parent.mkdir(parents=True, exist_ok=True)
        params = {'quality': 95, 'subsampling': 0}
        if exif:
            params['exif'] = exif
        if icc:
            params['icc_profile'] = icc
        img.save(dst, 'JPEG', **params)


def mirror_tree(src, out_dir, with_lpex=True, cover_ratio=0.4, dry_run=False,
                heic_mode=None, resume=False):
    """递归转换并镜像目录结构；无法转换或非实况的文件原样复制。"""
    stats = {'converted': 0, 'already': 0, 'copied': 0, 'skipped': 0,
             'heic': 0, 'resumed': 0, 'failed': [], 'heic_pairs': []}

    def source_done(dst, src_file):
        return dst.exists() and dst.stat().st_size == src_file.stat().st_size

    def live_done(dst):
        """输出是否为完整可用的实况图（按 XMP 里的视频长度校验文件尾部）。"""
        try:
            size = dst.stat().st_size
            if size <= 0:
                return False
            with open(dst, 'rb') as f:
                head = f.read(1024 * 1024)
                if b'GCamera:MotionPhoto="1"' not in head:
                    return False
                m = re.search(rb'OpCamera:VideoLength="(\d+)"', head)
                if not m:
                    return False
                vlen = int(m.group(1))
                if vlen <= 0 or vlen + 2 >= size:
                    return False
                f.seek(size - vlen - 2)
                if f.read(2) != b'\xff\xd9':
                    return False
                f.seek(size - vlen)
                return f.read(8)[4:] == b'ftyp'
        except OSError:
            return False

    dirs = sorted({p.parent for p in src.rglob('*') if p.is_file()})
    for d in dirs:
        entries = sorted(p for p in d.iterdir() if p.is_file())
        mp4s = {}
        for f in entries:
            if f.suffix.lower() == '.mp4':
                mp4s.setdefault(f.stem.lower(), f)
        consumed = set()

        # 第一轮：图片
        for f in entries:
            ext = f.suffix.lower()
            rel = f.relative_to(src)
            dst = out_dir / rel
            if ext == '.mp4':
                continue
            if f.name.lower() == '.nomedia':
                stats['skipped'] += 1
                print(f'[跳过 .nomedia] {rel}')
                continue
            if ext not in ('.jpg', '.jpeg'):
                paired = ext in HEIC_EXTS and f.stem.lower() in mp4s
                if paired and heic_mode is None:
                    if resume and source_done(dst, f):
                        stats['resumed'] += 1
                        print(f'[已完成-跳过] {rel}')
                        continue
                    stats['heic_pairs'].append(rel)
                    print(f'[提示] {rel} 有配对 mp4，HEIC 无法承载 OPPO 实况，两者均原样复制'
                          f'（可加 --heic-to-jpeg 转换）')
                    stats['copied'] += 1
                    if not dry_run:
                        copy_file(f, dst)
                    continue
                if ext in HEIC_EXTS and heic_mode and (paired or heic_mode == 'all'):
                    key = f.stem.lower()
                    clash = any(g is not f
                                and g.stem.lower() == key
                                and g.suffix.lower() in ('.jpg', '.jpeg')
                                for g in entries)
                    if clash:
                        stats['copied'] += 1
                        if not dry_run:
                            copy_file(f, dst)
                        print(f'[提示] {rel} 与同目录同名 jpg 冲突，未转换，原样复制')
                        continue
                    out_jpg = dst.with_suffix('.jpg')
                    if resume and (live_done(out_jpg) if paired else
                                   out_jpg.exists() and out_jpg.stat().st_size > 0):
                        if paired:
                            consumed.add(key)
                        stats['resumed'] += 1
                        print(f'[已完成-跳过] {rel}')
                        continue
                    try:
                        if not dry_run:
                            with tempfile.TemporaryDirectory() as td:
                                tmp = Path(td) / (key + '.jpg')
                                heic_to_jpeg(f, tmp)
                                if paired:
                                    convert(tmp, mp4s[key], out_jpg,
                                            with_lpex, cover_ratio)
                                else:
                                    copy_file(tmp, out_jpg)
                            os.utime(out_jpg, (f.stat().st_atime,
                                               f.stat().st_mtime))
                        if paired:
                            consumed.add(key)
                        stats['heic'] += 1
                        print(f'[HEIC→JPEG{"+实况" if paired else ""}] {rel}')
                    except Exception as e:  # noqa: BLE001
                        stats['failed'].append((rel, str(e)))
                        if not dry_run:
                            copy_file(f, dst)
                        print(f'[HEIC 转换失败-原样保留] {rel}: {e}')
                    continue
                if resume and source_done(dst, f):
                    stats['resumed'] += 1
                    print(f'[已完成-跳过] {rel}')
                    continue
                stats['copied'] += 1
                if not dry_run:
                    copy_file(f, dst)
                continue

            key = f.stem.lower()
            mp4 = mp4s.get(key)
            if resume and dst.exists() and dst.stat().st_size > 0 and (
                    live_done(dst) or dst.stat().st_size == f.stat().st_size):
                if mp4 is not None:
                    consumed.add(key)
                stats['resumed'] += 1
                print(f'[已完成-跳过] {rel}')
                continue
            if mp4 is None:
                stats['copied'] += 1
                if not dry_run:
                    copy_file(f, dst)
                print(f'[复制] {rel}')
                continue

            if is_oppo_live(f):
                consumed.add(key)
                stats['already'] += 1
                if not dry_run:
                    copy_file(f, dst)
                print(f'[已是实况] {rel}')
                continue

            try:
                if not dry_run:
                    jlen, vlen = convert(f, mp4, dst, with_lpex, cover_ratio)
                consumed.add(key)
                stats['converted'] += 1
                print(f'[实况] {rel}')
            except Exception as e:  # noqa: BLE001
                stats['failed'].append((rel, str(e)))
                if not dry_run:
                    copy_file(f, dst)
                print(f'[失败-原样保留] {rel}: {e}')

        # 第二轮：未被合并的 mp4
        for f in entries:
            if f.suffix.lower() != '.mp4':
                continue
            rel = f.relative_to(src)
            if f.stem.lower() in consumed:
                stats['skipped'] += 1
                print(f'[已合并-不复制] {rel}')
            else:
                dst = out_dir / rel
                if resume and source_done(dst, f):
                    stats['resumed'] += 1
                    print(f'[已完成-跳过] {rel}')
                    continue
                stats['copied'] += 1
                if not dry_run:
                    copy_file(f, dst)

    print(f'\n实况转换 {stats["converted"]}，已是实况 {stats["already"]}，'
          f'HEIC→JPEG {stats["heic"]}，原样复制 {stats["copied"]}，'
          f'已完成跳过 {stats["resumed"]}，其他跳过 {stats["skipped"]}，'
          f'失败 {len(stats["failed"])}')
    if stats['failed']:
        print('失败明细（均已原样复制原文件）：')
        for rel, msg in stats['failed']:
            print(f'  {rel}: {msg}')
    if stats['heic_pairs']:
        print(f'\n注意：发现 {len(stats["heic_pairs"])} 个 HEIC 带配对 mp4，'
              f'HEIC 无法做成 OPPO 实况，已把两者都原样复制'
              f'（如需转换请加 --heic-to-jpeg）：')
        for rel in stats['heic_pairs']:
            print(f'  {rel}')
    print(f'输出目录: {out_dir.resolve()}')


def main():
    ap = argparse.ArgumentParser(description='把 vivo 的 jpg+mp4 实况照片转成 OPPO 单文件实况照片')
    ap.add_argument('src', nargs='?', default='vivo_live', help='输入目录（默认 ./vivo_live）')
    ap.add_argument('-o', '--out', default='oppo_ready', help='输出目录（默认 ./oppo_ready）')
    ap.add_argument('--no-lpex', action='store_true', help='不写入 lpex 盒子（仅排障用）')
    ap.add_argument('--cover', type=float, default=0.4,
                    help='封面帧位置，视频时长的比例，0~1（默认 0.4）')
    ap.add_argument('--mirror', action='store_true',
                    help='递归处理子文件夹并保持目录结构，非实况/无法转换的文件原样复制')
    ap.add_argument('--dry-run', action='store_true', help='只打印计划，不写文件')
    ap.add_argument('--resume', action='store_true',
                    help='断点续跑：输出目录里已完整生成的文件直接跳过')
    ap.add_argument('--heic-to-jpeg', nargs='?', const='live',
                    choices=('live', 'all'), default=None,
                    help='把 HEIC/HEIF 转成 JPEG（会重新编码一次）；'
                         'live=只转带配对 mp4 的并做成实况（默认），all=全部 HEIC 都转')
    args = ap.parse_args()

    src = Path(args.src)
    out_dir = Path(args.out)
    if not src.is_dir():
        sys.exit(f'输入目录不存在: {src}')
    if out_dir.resolve().is_relative_to(src.resolve()):
        sys.exit('输出目录不能位于输入目录内部')
    if args.heic_to_jpeg and not args.mirror:
        sys.exit('--heic-to-jpeg 只在 --mirror 模式下生效')
    if args.resume and not args.mirror:
        sys.exit('--resume 只在 --mirror 模式下生效')

    if args.mirror:
        mirror_tree(src, out_dir, with_lpex=not args.no_lpex,
                    cover_ratio=args.cover, dry_run=args.dry_run,
                    heic_mode=args.heic_to_jpeg, resume=args.resume)
        return

    jpgs = sorted([p for p in src.iterdir()
                   if p.suffix.lower() in ('.jpg', '.jpeg')])
    if not jpgs:
        sys.exit(f'{src} 里没有 jpg')

    ok = skipped = failed = 0
    for jpg in jpgs:
        mp4 = jpg.with_suffix('.mp4')
        if not mp4.exists():
            print(f'[跳过] {jpg.name}：找不到同名 mp4')
            skipped += 1
            continue
        out_path = out_dir / (jpg.stem + '.jpg')
        try:
            jlen, vlen = convert(jpg, mp4, out_path,
                                 with_lpex=not args.no_lpex,
                                 cover_ratio=args.cover)
            ok += 1
            print(f'[完成] {jpg.name} + {mp4.name} -> {out_path}'
                  f'  (jpg {jlen/1048576:.1f}MB + video {vlen/1048576:.1f}MB)')
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f'[失败] {jpg.name}: {e}')

    print(f'\n完成 {ok}，跳过 {skipped}，失败 {failed}，输出目录: {out_dir.resolve()}')


if __name__ == '__main__':
    main()

/**
 * recorder.ts - a video of the backtest lab, saved with the session's snapshots.
 *
 * The browser's own screen capture of THIS tab (getDisplayMedia - the browser
 * asks which tab to share; pick this one), cropped to the lab's area with
 * Region Capture where the browser has it (Chrome and Edge), recorded with
 * MediaRecorder and streamed to the lab server every two seconds, which
 * writes it into runs/lab/<session>/snapshots/. Chunks go one after another,
 * never in parallel, so the file is written in the order it was recorded.
 *
 * MP4 when the browser can record it (Chrome and Edge 126+), otherwise WebM.
 * Nothing is converted afterwards: there is no ffmpeg on this machine.
 */
import { lab } from './labApi'

export type Recording = {
  /** The file it will be saved as. */
  file: string
  /** Stop recording; resolves once the file is saved. */
  stop: () => Promise<SavedVideo>
  /** Resolves when the file is saved - also when the browser's own "Stop sharing" ended it. */
  done: Promise<SavedVideo>
}
export type SavedVideo = { file: string; bytes: number; path: string } | { error: string }

const TYPES = ['video/mp4;codecs=avc1.640028', 'video/mp4;codecs=avc1', 'video/mp4',
  'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm']

export function canRecord(): boolean {
  return !!(navigator.mediaDevices as any)?.getDisplayMedia && typeof MediaRecorder !== 'undefined'
}

export async function startRecording(sid: string, area: HTMLElement): Promise<Recording> {
  const stream: MediaStream = await (navigator.mediaDevices as any).getDisplayMedia({
    video: { frameRate: 30, displaySurface: 'browser' },
    audio: false,
    // Offer this tab first; Chrome/Edge otherwise list every window.
    preferCurrentTab: true,
    selfBrowserSurface: 'include',
  })
  const track = stream.getVideoTracks()[0]
  // Only the lab, not the whole tab - where the browser supports Region
  // Capture and the tab shared is this one. Otherwise the whole surface.
  const CropTarget = (window as any).CropTarget
  if (CropTarget?.fromElement && (track as any)?.cropTo) {
    try { await (track as any).cropTo(await CropTarget.fromElement(area)) } catch { /* not this tab */ }
  }
  const mime = TYPES.find((t) => MediaRecorder.isTypeSupported(t)) ?? ''
  const ext: 'mp4' | 'webm' = mime.startsWith('video/mp4') ? 'mp4' : 'webm'
  let started: { id: string; file: string }
  try {
    started = await lab.recStart(sid, ext)
  } catch (e) {
    stream.getTracks().forEach((t) => t.stop())
    throw e
  }
  const rec = new MediaRecorder(stream, mime ? { mimeType: mime, videoBitsPerSecond: 8_000_000 }
    : { videoBitsPerSecond: 8_000_000 })
  let chain: Promise<unknown> = Promise.resolve()
  let failed: string | null = null
  rec.ondataavailable = (e) => {
    if (!e.data.size) return
    const part = e.data
    chain = chain.then(() => lab.recChunk(started.id, part)).catch((err) => {
      failed = failed ?? String(err?.message ?? err)
    })
  }
  const done = new Promise<SavedVideo>((resolve) => {
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop())
      await chain
      try {
        const r = await lab.recFinish(started.id)
        resolve(failed ? { error: `saved, but a part was lost: ${failed}` } : r)
      } catch (err: any) {
        resolve({ error: String(err?.message ?? err) })
      }
    }
  })
  // The browser's own "Stop sharing" ends the track: finish the file then too.
  track.addEventListener('ended', () => { if (rec.state !== 'inactive') rec.stop() })
  rec.start(2000)
  return {
    file: started.file,
    done,
    stop: () => {
      if (rec.state !== 'inactive') rec.stop()
      return done
    },
  }
}

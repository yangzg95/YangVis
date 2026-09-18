/** File System Access API 的最小环境声明。
 *
 * Chrome/Edge 专有，TS 5.6 的 lib.dom 只收录了 FileSystemHandle /
 * FileSystemWritableFileStream 等基础类型，缺 showSaveFilePicker 和
 * FileSystemHandle.remove()，这里用接口合并补上。哪天升级 TS 后 lib.dom
 * 自带了，这份文件可以整个删掉。
 */

interface FilePickerAcceptType {
  description?: string
  accept: Record<string, string[]>
}

interface SaveFilePickerOptions {
  suggestedName?: string
  types?: FilePickerAcceptType[]
  excludeAcceptAllOption?: boolean
}

interface Window {
  showSaveFilePicker(options?: SaveFilePickerOptions): Promise<FileSystemFileHandle>
}

interface FileSystemHandle {
  /** Chrome 110+。删除句柄指向的文件/目录；流式下载出错时用它清掉 0 字节占位文件。 */
  remove(options?: { recursive?: boolean }): Promise<void>
}

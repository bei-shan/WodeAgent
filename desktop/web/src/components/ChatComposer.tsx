import { useRef } from 'react'
import type { ChangeEvent, KeyboardEvent } from 'react'
import type { ModelInfo } from '../api'
import { Icons, Svg } from './icons'

type UploadedFile = { name: string; path: string }

interface ChatComposerProps {
  input: string
  onInput: (value: string) => void
  onSend: () => void
  sending: boolean
  models: ModelInfo[]
  selectedModel: string
  modelPickerOpen: boolean
  onToggleModelPicker: () => void
  onChooseModel: (model: ModelInfo) => void
  uploadedFiles: UploadedFile[]
  onRemoveFile: (path: string) => void
  onFileUpload: (event: ChangeEvent<HTMLInputElement>) => void
  planMode: boolean
  onTogglePlanMode: () => void
  agentTeams: boolean
  onToggleTeams: () => void
  centered?: boolean
}

export default function ChatComposer({
  input, onInput, onSend, sending, models, selectedModel, modelPickerOpen,
  onToggleModelPicker, onChooseModel, uploadedFiles, onRemoveFile, onFileUpload,
  planMode, onTogglePlanMode, agentTeams, onToggleTeams, centered = false,
}: ChatComposerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      onSend()
    }
  }

  return (
    <div className={`chat-composer ${centered ? 'chat-composer-centered' : ''}`}>
      <textarea
        aria-label="向 Ridge 发送消息"
        placeholder="问任何问题"
        value={input}
        onChange={event => onInput(event.target.value)}
        onKeyDown={handleKeyDown}
        rows={2}
        disabled={sending}
        className="chat-composer-input"
      />
      {uploadedFiles.length > 0 && (
        <div className="composer-attachments" aria-label="待发送的附件">
          {uploadedFiles.map(file => (
            <span key={file.path} className="composer-attachment">
              <Svg d={Icons.file} size={14} />
              <span className="truncate max-w-36">{file.name}</span>
              <button type="button" aria-label={`移除附件 ${file.name}`}
                onClick={() => onRemoveFile(file.path)}>×</button>
            </span>
          ))}
        </div>
      )}
      <div className="composer-toolbar">
        <div className="composer-actions">
          <input ref={fileInputRef} type="file" multiple className="sr-only"
            aria-label="上传文件" onChange={onFileUpload} tabIndex={-1} />
          <button type="button" className="composer-icon-button"
            onClick={() => fileInputRef.current?.click()} disabled={sending}
            aria-label="添加文件" title="添加文件">
            <Svg d={Icons.plus} size={18} />
          </button>
          <div className="relative">
            <button type="button" onClick={onToggleModelPicker}
              className="composer-option" aria-haspopup="listbox" aria-expanded={modelPickerOpen}>
              <Svg d={Icons.brain} size={15} />
              <span className="model-label">{selectedModel || models[0]?.model || '选择模型'}</span>
              <Svg d={Icons.chevron} size={13} />
            </button>
            {modelPickerOpen && (
              <div className="model-menu" aria-label="选择模型">
                {models.length === 0 ? (
                  <p className="px-3 py-2 text-xs text-gray-500">暂无可用模型</p>
                ) : models.map(model => (
                  <button key={model.name} type="button" aria-label={model.model}
                    aria-pressed={(selectedModel || models[0]?.model) === model.model}
                    onClick={() => onChooseModel(model)}
                    className="model-menu-option">
                    <span className="font-medium">{model.model}</span>
                    <span className="text-xs text-gray-400">{model.provider}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <button type="button" className={`composer-option toggle-option ${planMode ? 'selected' : ''}`}
            aria-pressed={planMode} onClick={onTogglePlanMode} title="先规划再执行">
            <Svg d={Icons.check} size={14} /> <span>Plan</span>
          </button>
          <button type="button" className={`composer-option toggle-option ${agentTeams ? 'selected' : ''}`}
            aria-pressed={agentTeams} onClick={onToggleTeams} disabled={planMode}
            title={planMode ? '计划模式下无法启用 Agent 团队' : '启用多 Agent 协作'}>
            <Svg d={Icons.teams} size={15} /> <span>团队</span>
          </button>
        </div>
        <button type="button" className="composer-send" onClick={onSend}
          disabled={sending || (!input.trim() && uploadedFiles.length === 0)} aria-label="发送消息" title="发送消息 (Enter)">
          <Svg d={Icons.send} size={18} />
        </button>
      </div>
    </div>
  )
}

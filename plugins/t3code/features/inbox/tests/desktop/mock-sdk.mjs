// Minimal @hermes/plugin-sdk stand-in for behaviour tests of features/inbox/desktop.js.
import React from 'react'
import { atom } from 'nanostores'

export { atom }

export const calls = { notify: [], open: [], patch: [], pin: [], reorder: [], requests: [], rest: [] }

export const host = {
  state: {
    connectionId: atom(null),
    focusedSessionOwner: atom(null),
    focusedStoredSessionId: atom(null),
    gateway: atom('open'),
    profile: atom('default')
  },
  sessions: {
    pin: (id, pinned, index) => calls.pin.push({ id, index, pinned }),
    reorderPinned: ids => calls.reorder.push(ids)
  },
  notify: n => calls.notify.push(n),
  notifyError: (err, msg) => calls.notify.push({ kind: 'error', message: msg, err }),
  openSession: (id, opts) => {
    calls.open.push({ id, opts })

    return Promise.resolve()
  },
  newChat: profile => calls.open.push({ newChat: profile }),
  revealPane: () => {},
  navigate: () => {},
  profiles: { list: () => Promise.resolve({ profiles: [] }) },
  // Tests replace these per scenario.
  request: async () => ({ sessions: [] }),
  requestProfile: async () => ({ sessions: [] })
}

const queryData = new Map()
const keyStr = key => JSON.stringify(key)

export const queryClient = {
  getQueryData: key => queryData.get(keyStr(key)),
  setQueryData: (key, fn) => queryData.set(keyStr(key), typeof fn === 'function' ? fn(queryData.get(keyStr(key))) : fn),
  invalidateQueries: () => Promise.resolve(),
  _clear: () => queryData.clear()
}

export const useQuery = ({ queryKey }) => {
  const data = queryData.get(keyStr(queryKey))

  return { data, error: null, isFetching: false, isLoading: false, refetch: () => {} }
}

export const useValue = store =>
  React.useSyncExternalStore(
    cb => store.listen(cb),
    () => store.get(),
    () => store.get()
  )

export const cn = (...xs) => xs.filter(Boolean).join(' ')
export const profileColor = () => null
export const profileColorSoft = () => null

const pass = ({ children }) => (children === undefined ? null : React.createElement(React.Fragment, null, children))
export const ContextMenu = pass
export const ContextMenuContent = pass
export const ContextMenuItem = pass
export const ContextMenuSeparator = () => null
export const ContextMenuSub = pass
export const ContextMenuSubContent = pass
export const ContextMenuSubTrigger = pass
export const ContextMenuTrigger = pass
export const DropdownMenu = pass
export const DropdownMenuContent = pass
export const DropdownMenuItem = pass
export const DropdownMenuSeparator = () => null
export const DropdownMenuTrigger = pass
export const Popover = pass
export const PopoverContent = pass
export const PopoverTrigger = pass
export const Dialog = ({ open, children }) => (open ? React.createElement(React.Fragment, null, children) : null)
export const DialogContent = pass
export const DialogFooter = pass
export const DialogHeader = pass
export const DialogTitle = pass
export const Button = ({ children }) => React.createElement('button', null, children)
export const Input = () => React.createElement('input')
export const Tip = pass
export const ToggleRow = ({ label }) => React.createElement('div', null, label)
export const ListRow = ({ title, description }) => React.createElement('div', null, title, description)
export const SegmentedControl = () => null
export const STATUSBAR_AREAS = { right: 'statusBar.right' }

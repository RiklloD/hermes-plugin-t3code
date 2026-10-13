import React from 'react'
import { atom } from 'nanostores'
export { atom }
export const fixture = { info: null, calls: [], result: {}, confirmed: false }
export const host = {
  state: {
    focusedSessionId: atom('runtime-a'),
    focusedSessionOwner: atom({ connectionId: 'local', profile: 'owner-a' }),
    busy: atom(false), model: atom('claude-sonnet-5-5')
  },
  requestProfile: async (route, method, params) => {
    fixture.calls.push({ route, method, params })
    return fixture.result
  },
  notify: value => fixture.calls.push({ notification: value })
}
export const useValue = store => React.useSyncExternalStore(cb => store.listen(cb), () => store.get(), () => store.get())
export const useQuery = () => ({ data: fixture.info })
export const queryClient = { invalidateQueries: async () => {} }
export const surfaceModelSwitchConfirm = async options => {
  fixture.confirmed = true
  if (options.isStale()) return false
  await options.requestConfirmed()
  return true
}
const pass = ({ children }) => React.createElement(React.Fragment, null, children)
export const DropdownMenu = pass
export const DropdownMenuTrigger = pass
export const DropdownMenuContent = pass
export const DropdownMenuSeparator = () => null
export const DropdownMenuItem = ({ children, onSelect, disabled }) => React.createElement('button', { disabled, onClick: onSelect }, children)

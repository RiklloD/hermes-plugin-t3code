export const calls = []
export let control = null
export function setControl(value) { control = value }
const atom = value => ({ get: () => value, set: next => { value = next } })
export const host = { state: {
  focusedStoredSessionId: atom('thread'), focusedSessionOwner: atom({ connectionId: 'local', profile: 'test' }),
  focusedSessionId: atom('live'), model: atom('api-model')
}, requestProfile: async (route, method, params) => { calls.push({ route, method, params }); return { control: { goal: control } } } }
export const useValue = value => value.get()
export let query = { data: null, isError: false, refetch: () => {} }
export function setQuery(value) { query = value }
export const useQuery = () => query
export const queryClient = { invalidateQueries() {} }
export const COMPOSER_AREAS = { top: 'composer.top' }
export const Tip = ({ children }) => children

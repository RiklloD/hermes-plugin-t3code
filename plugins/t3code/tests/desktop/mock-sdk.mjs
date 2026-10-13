// @hermes/plugin-sdk stand-in for the T3 Code isolation tests: the Inbox test mock plus the
// areas and interactive controls the shell and the other features use.
import React from 'react'

export * from '../../features/inbox/tests/desktop/mock-sdk.mjs'

export const ROUTES_AREA = 'routes'
export const PALETTE_AREA = 'palette'
export const SESSION_ROW_AREAS = { trailing: 'sessionRow.trailing' }
export const COMPOSER_AREAS = { top: 'composer.top', actions: 'composer.actions' }
export const Button = ({ children, onClick }) => React.createElement('button', { onClick }, children)
export const ToggleRow = ({ label, description, checked, onChange }) =>
  React.createElement(
    'label',
    { 'data-toggle': label },
    React.createElement('input', { checked: Boolean(checked), onChange: e => onChange?.(e.target.checked), type: 'checkbox' }),
    label,
    ' — ',
    description
  )
export const ListRow = ({ title, description, action }) => React.createElement('div', null, title, description, action)

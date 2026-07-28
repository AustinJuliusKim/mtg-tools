import { useCallback, useMemo, useState } from 'react'
import type { Filters, Holding, Selection } from '../api/client'

/**
 * Row selection for the collection table.
 *
 * The distinction this exists to protect: **"the rows on this page" and
 * "everything matching this filter" are different things**, and at collection
 * scale they differ by hundreds of rows. The UI must never blur them, and the
 * request must never turn the second into a materialized id list — the server
 * re-resolves `selectAll` against the filter, so a filter that changed since
 * render cannot silently widen the edit.
 *
 * Kept out of the component and unit-tested because this is the one piece of
 * UI where a bug becomes a data bug.
 */
export interface SelectionState {
  /** Rows ticked on the current page. */
  picked: Holding[]
  /** True when the user escalated to "everything matching the filter". */
  allMatching: boolean
  /** How many rows an action would touch, for the confirmation. */
  count: number
  /** True when every row on this page is ticked. */
  pageFull: boolean
  /** Whether offering the escalation would actually add rows. */
  canEscalate: boolean
  setPicked: (rows: Holding[]) => void
  escalate: () => void
  collapseToPage: () => void
  clear: () => void
  /** What goes on the wire. */
  toRequest: () => Selection
}

export function useSelection(
  pageRows: Holding[],
  totalMatching: number,
  filters: Filters,
): SelectionState {
  const [picked, setPickedState] = useState<Holding[]>([])
  const [allMatching, setAllMatching] = useState(false)

  const setPicked = useCallback((rows: Holding[]) => {
    setPickedState(rows)
    // Any manual change drops the escalation: the user is now talking about
    // specific rows again.
    setAllMatching(false)
  }, [])

  const escalate = useCallback(() => setAllMatching(true), [])
  const collapseToPage = useCallback(() => setAllMatching(false), [])
  const clear = useCallback(() => {
    setPickedState([])
    setAllMatching(false)
  }, [])

  const pageFull = pageRows.length > 0 && picked.length === pageRows.length
  const canEscalate = !allMatching && pageFull && totalMatching > pageRows.length
  const count = allMatching ? totalMatching : picked.length

  const toRequest = useCallback((): Selection => {
    if (allMatching) {
      // Deliberately no ids: the server resolves the filter itself.
      return { selectAll: true, filters }
    }
    return { ids: picked.map((row) => row.id) }
  }, [allMatching, filters, picked])

  return useMemo(
    () => ({
      picked,
      allMatching,
      count,
      pageFull,
      canEscalate,
      setPicked,
      escalate,
      collapseToPage,
      clear,
      toRequest,
    }),
    [
      picked,
      allMatching,
      count,
      pageFull,
      canEscalate,
      setPicked,
      escalate,
      collapseToPage,
      clear,
      toRequest,
    ],
  )
}

"""The workspace: one page, four tabs.

    Drawings  the set — what state each drawing is in, and one drawing up close
    Queue     what the worker is reading, with pause, stop, cancel and retry
    BOQ       the combined bill from every read drawing, and the project estimate
    Pricing   rates against the project estimate

These used to be five pages plus a view that changed shape depending on how
many boxes were ticked: none showed the queue, one showed a single drawing, two
or more showed a batch — and ticking a second drawing silently threw away the
one being reviewed. Selecting and opening are separate now. Ticking a drawing
only selects it for a batch action; "Open" is what shows it up close.

Each tab is a module with a `render()` function rather than a page script, so
it can be imported, tested and guarded: a failure in one tab is reported in
words inside that tab, and the other three keep working.
"""

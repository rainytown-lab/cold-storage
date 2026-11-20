# Cold Storage

Inventory for a lab's freezers, fridges and liquid nitrogen tanks. Zero
dependencies: Python 3 standard library (`http.server` + `sqlite3`) and a
vanilla-JS single-page app. No build step, no pip, no npm.

## Run

```bash
python3 app.py              # http://localhost:8100
PORT=9000 python3 app.py
COLDSTORAGE_DB=/path/to.db python3 app.py
```

The first visit asks you to create the lab and its first (admin) account. Everyone
else joins with the **lab code**: they choose "Join a lab" on the sign-in page, type
the code and make their own account. Any member can see the code under
**Settings › Lab**. Only an admin can change it or generate a new one (people who
already joined keep their accounts). An admin can also add people directly
under **Settings › People**, with a temporary password. Sign out from the top bar.

## What it does

- **Storage units**: LN₂, −80 °C, −20 °C, +4 °C. Each one has a name, location,
  size, manufacturer, model, serial number, inventory number, date of last defrost,
  date of last PM, notes, and an owner (or **Common**).
- **Presets**: when you add a unit, you can start from a ready-made set-up for a
  common model. It fills in the type, manufacturer, device name, model, size and
  layout, and everything stays editable. The built-in preset is the Thermo
  CryoExtra CE8120 LN₂ tank: four compartments (A–D) of three racks each, drawn in
  their real places. Any unit can be saved as a new preset ("Save as preset" on
  its page). Presets are managed under Settings.
- **Arranged tanks**: in a tank whose racks sit in fixed compartment slots,
  dragging a rack onto another rack swaps them, and dragging it onto an empty slot
  moves it there. The rack page has a matching Slot picker.
- **Liquid nitrogen tanks have no shelves or rows**: a tank holds racks, and each
  rack is a vertical stack of boxes (box 1 at the top). You set racks and boxes per
  rack when you add a tank. Tanks made by earlier versions are converted
  automatically on startup, and their items stay in their boxes.
- **Freezer/fridge layout**: unit › shelf › rack › row › box › position. You set the counts when
  you create a unit, and a live preview shows the result. After that you can add,
  delete, rename or reassign any shelf, rack, row or box, and resize a box
  (e.g. 9×9, 10×10, 8×12, or a custom size; positions labelled A1… or 1…n).
- **Interactive diagram**: freezers and fridges show a tall, freezer-shaped front
  view (racks, rows and drawers are drawn portrait too); LN₂ tanks a top
  view. You click into a rack, then a row, then a box, then a position. Each level
  shows only a summary of the level below (name and how full it is). The tubes,
  coloured by category, appear only once you open a box. Pieces on a shelf, and
  boxes in a row, always stay on one line in their real order and proportions,
  whatever the window width. "Highlight" dims everything
  that doesn't belong to the chosen person (or to Common).
- **Assignments**: a person or **Common** can be set on a unit, shelf, rack, row,
  box or a single item. Anything left unassigned inherits from the level above it.
- **Items**: name, category, owner, quantity, concentration, source, lot/passage,
  expiry, date added, notes. Click an empty position to add an item, and use
  "Number of tubes" to fill the next N positions with aliquots. Shift-click (or
  "Select positions") selects several at once to add or remove in bulk.
- **Loose items**: things that aren't in a box can sit in the unit itself, on a
  shelf, or in a **loose space**, with a free-text spot ("door", "bottom bin").
- **Loose spaces**: a loose space is room on a shelf for bottles, bags and kits.
  It sits in line with the racks and is 1–N rack-widths wide. Open an empty rack
  and click "Replace with loose space" (or the reverse) to swap one for the other
  in the same spot.
- **Selecting several racks**: shift-click racks or loose spaces (or click "Select
  racks" and then click them) to select several on one shelf or in one tank.
  "Make one loose space" turns side-by-side selected pieces into a single loose space
  as wide as all of them (the racks must be empty). "Make into racks" turns
  selected loose spaces back into racks, one per rack-width. Dragging any selected
  piece moves the whole group, in order.
- **Editing the layout**: a unit's Details › Edit page lists what's on each shelf
  (or in the tank) and has "+ Add shelf" / "+ Add rack", "+ Add loose space",
  "Save as preset" and Delete. Individual pieces are changed from the diagram.
- **Merging loose spaces**: when two loose spaces sit side by side, either
  one's panel offers "Merge with …". The left one stays, the widths add up, and
  the other's items move over. Arranged tanks have fixed slots, so their spaces
  can't be merged.
- **Navigation**: a "← Back to …" button in the top-left corner goes up one level,
  with the breadcrumb beside it.
- **Drag & drop**: drag racks and loose spaces to reorder them or move them to
  another shelf. This works in the freezer front view and the LN₂ top view; on a
  phone, long-press first. A rack or space keeps its name when it moves. The side
  panel also has "Move left / right" and "Shelf" controls.
- **You are here**: inside a shelf, rack, row or box, a small freezer-shaped map
  in the header highlights your shelf and rack. Click it to jump.
- **Move**: open an item, click Move, then click an empty position in any box in
  any unit, or put it somewhere as a loose item.
- **Remove / put back**: removal records the date, the person and a reason. Removed
  items stay searchable and keep their last location.
- **Search** across all storage by text (it also matches the location path, e.g.
  "Freezer A Rack 2"), category, owner, unit or unit type, and status. Results
  export to CSV.
- **Log**: every add, remove, move, edit and layout change, with who did it and
  when. Each item also has its own history.
- **Categories**: Cells, Plasmids, Antibodies, Glycerol stocks, DNA, RNA, Protein,
  … You can add, rename and recolour them under Settings.

## Files

- `app.py`: HTTP server and JSON API
- `db.py`: SQLite schema
- `static/`: `index.html`, `app.js`, `style.css`

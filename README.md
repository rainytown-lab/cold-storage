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

The first visit asks you to create the lab and its first (admin) account. The
admin adds everyone else under **Settings › People**.

## What it does

- **Storage units**: LN₂, −80 °C, −20 °C, +4 °C. Each one has a name, location,
  size, manufacturer, model, serial number, inventory number, date of last defrost,
  date of last PM, notes, and an owner (or **Common**).
- **Layout**: unit › shelf › rack › row › box › position. You set the counts when
  you create a unit, and a live preview shows the result. After that you can add,
  delete, rename or reassign any shelf, rack, row or box, and resize a box
  (e.g. 9×9, 10×10, 8×12, or a custom size; positions labelled A1… or 1…n).
- **Interactive diagram**: freezers and fridges show a front view, LN₂ tanks a top
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
- **Drag & drop**: drag racks and loose spaces to reorder them or move them to
  another shelf. This works in the freezer front view and the LN₂ top view; on a
  phone, long-press first. A rack or space keeps its name when it moves. The side
  panel also has "Move left / right" and "Shelf" controls.
- **You are here**: inside a shelf, rack, row or box, a mini map in the header
  highlights where you are in the unit (and in the rack). Click it to jump.
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

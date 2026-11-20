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
- **Containers in loose spaces**: a loose space can hold containers, such as a Box,
  a Freezing cylinder or a Bag, and loose items go into those containers (or
  straight into the space). Each container has its own page (name, type, owner,
  items, move to another loose space). Container types are an editable list under
  Settings › Container types; renaming a type renames it on every container.
- **Arranged tanks**: in a tank whose racks sit in fixed compartment slots,
  dragging a rack onto another rack swaps them, and dragging it onto an empty slot
  moves it there. The rack page has a matching Slot picker.
- **Liquid nitrogen tanks have no shelves or rows**: a tank holds racks, and each
  rack is a vertical stack of boxes (box 1 at the top). You set racks and boxes per
  rack when you add a tank. Tanks made by earlier versions are converted
  automatically on startup, and their items stay in their boxes.
- **Fridges without racks**: when adding a unit, choose what goes on each shelf (or
  in a tank): **Racks**, **Loose space only** (e.g. an under-counter fridge or a dry
  shipper), or **Both**. Loose space is set up as a number of sections per shelf,
  each optionally starting with a number of containers of one type (e.g. 3 Boxes).
  "+4 °C fridge" defaults to loose space. "+ Add shelf" offers the same, and
  "+ Add container" can add several at once.
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
- **Items**: choose the **category first** when adding an item; the form then
  shows that category's own fields alongside name, owner, quantity, date added,
  expiry and notes. Built-in examples: Primers (sequence, direction, target gene,
  species, concentration, Tm, supplier), Cells (date frozen, passage, vessel size,
  cells per vial…), Antibodies (brand, catalog number, host, target, clone,
  conjugate, isotype…), plus sets for plasmids, glycerol stocks, DNA, RNA, protein,
  tissue, serum and reagents. Each category's fields can be edited under
  Settings › Item categories › Fields… (text, long text, number, date or dropdown).
  These details show on the item's page, are searchable, and are exported to CSV.
  Click an empty position to add an item; "Number of tubes" fills the next N
  positions with aliquots. Shift-click (or "Select positions") selects several at
  once to add or remove in bulk.
- **Loose items**: things that aren't in a box always live in a **loose space**,
  a drawn section of the shelf or tank (like a tube lives in a box), with an
  optional free-text spot ("door", "bottom bin"). Loose items left outside a loose
  space by older versions are moved into a new loose space on their shelf (or in
  the tank) when the server starts.
- **Loose spaces**: a loose space is room for bottles, bags and kits. It can stand
  wherever a rack, row or box would: on a shelf or in a tank (in place of racks), in
  a freezer rack (in place of rows), or in a row or LN₂ rack (in place of boxes). It
  is 1–N racks wide, rows high, or boxes wide. Any empty rack, row or box can be
  replaced with a loose space ("Replace with loose space" on its page), and a loose
  space can be turned back into whatever stands beside it.
- **Same-size racks**: every rack in a unit is drawn at the same width, set by
  the shelf with the most rack-widths. Spare room on other shelves shows as a
  dashed "Empty" slot; click it to add a rack or a loose space there. Boxes in a
  freezer rack are the same width across its rows too.
- **Selecting several racks**: shift-click racks or loose spaces (or click "Select
  racks" and then click them) to select several on one shelf or in one tank.
  "Make loose space…" offers one loose space as wide as all of them (side-by-side
  pieces only) or a separate loose space for each rack (the racks must be empty
  either way). "Make into racks" turns selected loose spaces back into racks, one
  per rack-width. "Assign…" gives them all to a person or to Common, or puts them
  back to inheriting. Dragging any selected piece moves the whole group, in order.
- **Editing the layout**: a unit's Details › Edit page lists what's on each shelf
  (or in the tank) and has "+ Add shelf" / "+ Add rack", "+ Add loose space",
  "Save as preset" and Delete. Individual pieces are changed from the diagram.
- **Merging loose spaces**: when two loose spaces sit side by side, either
  one's panel offers "Merge with …". The left one stays, the widths add up, and
  the other's items move over. Arranged tanks have fixed slots, so their spaces
  can't be merged.
- **Navigation**: a "← Back to …" button in the top-left corner goes up one level,
  with the breadcrumb beside it.
- **Drag & drop**: everything is positioned by dragging. Drag racks and loose
  spaces along a shelf or to another shelf (in an LN₂ tank, around the tank), rows
  up and down in a rack (by their label), boxes along a row or into another row, and
  containers within a loose space. On a phone, long-press first. Pieces keep their
  names when they move; if the name is taken where they land, they get the next
  free number.
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
- **Undo**: most entries in the Log have an "↶ Undo" button. One action is undone
  as a whole, even if it wrote several log lines (adding 3 aliquots, removing 5
  tubes, converting 4 racks). Undo goes newest-first: if something later touched
  the same item, or the same unit for layout changes, undo that first. Changes
  made before this feature existed (and deleting a whole unit) can't be undone.
- **Categories**: Cells, Plasmids, Antibodies, Glycerol stocks, DNA, RNA, Protein,
  … You can add, rename and recolour them under Settings.

## Files

- `app.py`: HTTP server and JSON API
- `db.py`: SQLite schema
- `static/`: `index.html`, `app.js`, `style.css`

## Live site (Fly.io)

The lab's copy runs on Fly.io as app `coldstorage` (https://coldstorage.fly.dev), with
the database on a persistent volume at `/data/coldstorage.db`. Code lives in the
private GitHub repo `rainytown-lab/cold-storage`.

```bash
fly deploy                      # ship the current code (from this folder)
fly logs                        # watch the live server
fly ssh sftp get /data/coldstorage.db   # download a copy of the live database
```

Keep it to one machine (SQLite has a single writer). The database is never part of
the image or the repo.

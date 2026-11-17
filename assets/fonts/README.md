# Font

Opsional. Kalau folder ini berisi file `.ttf` / `.otf`, file itu dipakai untuk thumbnail dan subtitle
(file yang namanya mengandung *Black*, *ExtraBold*, atau *Bold* didahulukan).

Kalau kosong, pipeline memakai font sistem. Di GitHub Actions yang dipakai adalah Roboto dan DejaVu Sans,
yang dipasang lewat `apt`.

Pakai hanya font berlisensi bebas (mis. SIL Open Font License dari Google Fonts). Nama keluarga font
untuk subtitle diatur di `config.yaml` → `styles.caption_styles[].font`.

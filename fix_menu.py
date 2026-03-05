
with open(r'c:\vibecoding_work\claredex\index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the first </header> - this is the correct closing tag of our header
first_header_close = content.find('</header>')

# Find the section start - the hero/about section
section_start = content.find('<!-- ========== HERO / ABOUT SECTION')

# If we found both, delete everything between first </header> and section start
if first_header_close != -1 and section_start != -1:
    end_of_first_header = first_header_close + len('</header>')
    new_content = content[:end_of_first_header] + '\n\n\n' + content[section_start:]
    with open(r'c:\vibecoding_work\claredex\index.html', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print("Done! Removed", section_start - end_of_first_header, "characters of orphaned menu HTML")
else:
    print("Could not find markers. first_header_close:", first_header_close, "section_start:", section_start)

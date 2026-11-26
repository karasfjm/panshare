package service

import "testing"

func TestTMDBWebBackdropRegex(t *testing.T) {
	html := `<img src="https://media.themoviedb.org/t/p/w1920_and_h600_multi_faces_filter(duotone,00192f,00baff)/abc123.jpg">` +
		`<img src="https://media.themoviedb.org/t/p/w1920_and_h600_multi_faces_filter(duotone,00192f,00baff)/def456.jpg">` +
		`<img src="https://media.themoviedb.org/t/p/w1920_and_h600_multi_faces_filter(duotone,00192f,00baff)/abc123.jpg">`
	got := tmdbWebBackdrop.FindAllStringSubmatch(html, -1)
	if len(got) != 3 {
		t.Fatalf("want 3 matches, got %d", len(got))
	}
	if got[0][1] != "abc123.jpg" {
		t.Fatalf("want abc123.jpg, got %s", got[0][1])
	}
}

func TestSharedProxyPriority(t *testing.T) {
	if got := sharedProxy(" http://page:7890 ", "http://compose:7891"); got != "http://page:7890" {
		t.Fatalf("page proxy should win, got %q", got)
	}
	if got := sharedProxy("  ", "http://compose:7891"); got != "http://compose:7891" {
		t.Fatalf("compose proxy should be used, got %q", got)
	}
}

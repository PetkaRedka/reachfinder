package b

import "example.com/c"

func Mid() string {
	return c.Vuln()
}

package a

import "example.com/b"

func Top() string {
	return b.Mid()
}

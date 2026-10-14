module example.com/demo

go 1.21

require example.com/a v0.0.0

require (
	example.com/b v0.0.0 // indirect
	example.com/c v0.0.0 // indirect
)

replace example.com/a => ../a

replace example.com/b => ../b

replace example.com/c => ../c

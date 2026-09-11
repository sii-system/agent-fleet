package main

import (
	"fmt"
	"os"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] != "quote\" slash\\ unicode☃" {
		panic("argv changed")
	}
	wd, err := os.Getwd()
	if err != nil {
		panic(err)
	}
	fmt.Printf("PROBE_OK uid=%d cwd=%s\n", os.Getuid(), wd)
}

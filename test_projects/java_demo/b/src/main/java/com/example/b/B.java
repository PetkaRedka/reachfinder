package com.example.b;

import com.example.c.C;

public class B {
    public static String mid() {
        return C.vuln();
    }
}
